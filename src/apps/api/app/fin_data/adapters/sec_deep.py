"""Deep SEC EDGAR parsing engine (Phase A1).

Provides 4 deterministic, strictly typed SEC capabilities reusing ``SecEdgarAdapter``
conventions (policy gates, declared User-Agent, rate limiter, injectable Transport):
1. ``sec_earnings_release``: Dual-period (current & prior quarter) 8-K Item 2.02
   EX-99.1 / optional EX-99.2 retrieval via directory ``index.json``, table-preserving
   HTML extraction, and hints for management summary, Non-GAAP reconciliations, and
   forward guidance (excluding Safe Harbor boilerplate).
2. ``sec_filing_tables`` & ``fetch_filing_table``: ``FilingSummary.xml`` report catalog
   grouped by category (``Statements`` / ``Notes`` / ``Tables``) + deterministic
   ``R*.htm`` table parser extracting rows, columns, periods, unit/scale, currency,
   XBRL tags, and provenance metadata.
3. ``sec_filing_sections`` & ``sec_redline_diff``: Deterministic 10-K / 10-Q Item
   section splitter (``Item 1A Risk Factors``, ``Item 2/7 MD&A``,
   ``Liquidity and Capital Resources``, ``Critical Accounting Estimates``) +
   cross-period Redline Diff filtering pure date/number rolling noise.
4. ``cik_scoped_efts_search``: Strictly single-CIK-scoped wrapper around SEC EFTS
   full-text search (never runs an unscoped global query).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence
from urllib.parse import quote, urlencode, urlsplit
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from ...openai_compatible import _TransportFailure, _TransportTimeout
from ..models import (
    Coverage,
    Cost,
    Filing,
    FullTextHit,
    Permissions,
    Provenance,
    SuggestedAction,
    ToolResult,
)
from ..policy import Denied, FinDataPolicy
from .base import (
    MAX_BYTES,
    StopWith,
    StreamTransport,
    Transport,
    header as http_header,
    rate_limited_result,
    retry_after,
    utcnow,
)
from .sec_edgar import EFTS_URL, PROVIDER, SecEdgarAdapter, _CappedSink, cik10, cik_entity_id

SEC_ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "efts.sec.gov"})
MAX_EXHIBIT_BYTES = 15_000_000
MAX_FILING_HTML_BYTES = 60_000_000
MAX_XML_BYTES = 10_000_000

_ACCESSION_RE = re.compile(r"^(\d{10})-?(\d{2})-?(\d{6})$")
_R_FILE_RE = re.compile(r"^[Rr]\d{1,5}\.html?$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$")

_HARD_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "svg", "ix:header", "ix:hidden", "template"})
_VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCK_TAGS = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "section", "article", "header", "footer"})
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_HIDDEN_STYLE_RE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!\s*important\s*)?(?:;|$)", re.IGNORECASE)
_META_CHARSET_RE = re.compile(rb"<meta\b[^>]*\bcharset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
_XML_ENCODING_RE = re.compile(rb"<\?xml\b[^>]*\bencoding\s*=\s*['\"]([A-Za-z0-9._-]+)['\"]", re.IGNORECASE)
_CHARSET_HEADER_RE = re.compile(r";\s*charset\s*=\s*['\"]?([A-Za-z0-9._-]+)['\"]?", re.IGNORECASE)
_DTD_OR_ENTITY_RE = re.compile(r"<!(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)

_EX99_1_NAME_RE = re.compile(
    r"(?:ex(?:h(?:ibit)?)?[-_.]?99[-_.]?1|(?<![0-9])99[-_.]1)(?:[^0-9]|$)",
    re.IGNORECASE,
)
_EX99_2_NAME_RE = re.compile(
    r"(?:ex(?:h(?:ibit)?)?[-_.]?99[-_.]?2|(?<![0-9])99[-_.]2)(?:[^0-9]|$)",
    re.IGNORECASE,
)
_EX99_1_TYPE_RE = re.compile(r"^EX-?99\.0*1$", re.IGNORECASE)
_EX99_2_TYPE_RE = re.compile(r"^EX-?99\.0*2$", re.IGNORECASE)
_HTML_EXT_RE = re.compile(r"\.(?:html?|txt)$", re.IGNORECASE)

_SHOW_AR_RE = re.compile(r"Show\.showAR\s*\([^,]+,\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)

_CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
_ISO_CURRENCIES = ("USD", "EUR", "GBP", "JPY", "CAD", "CHF", "AUD", "CNY", "RMB", "HKD", "KRW", "TWD", "SGD")

_MONTH_PATTERN = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


# --------------------------------------------------------------------------- Models
class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ParsedHtmlTable(_Frozen):
    title: str | None = None
    headers: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    raw_markdown: str = ""
    is_nongaap: bool = False


class EarningsReleaseExhibit(_Frozen):
    exhibit_type: Literal["EX-99.1", "EX-99.2"]
    filename: str
    url: str
    description: str | None = None
    text: str
    tables: list[ParsedHtmlTable] = Field(default_factory=list)
    content_sha256: str
    provenance: Provenance | None = None


class NonGaapReconciliation(_Frozen):
    title: str | None = None
    text: str = ""
    table: ParsedHtmlTable | None = None
    metrics_mentioned: list[str] = Field(default_factory=list)
    source_exhibit: str = "EX-99.1"


class GuidancePassage(_Frozen):
    heading: str | None = None
    text: str
    horizon: str | None = None
    metrics_mentioned: list[str] = Field(default_factory=list)
    source_exhibit: str = "EX-99.1"


class EarningsReleasePeriod(_Frozen):
    period_role: Literal["current", "prior"]
    accession: str
    filed_at: date
    accepted_at: datetime | None = None
    period_of_report: date | None = None
    form: str = "8-K"
    items: list[str] = Field(default_factory=list)
    index_json_url: str
    ex99_1: EarningsReleaseExhibit | None = None
    ex99_2: EarningsReleaseExhibit | None = None
    management_summary: str = ""
    management_highlights: list[str] = Field(default_factory=list)
    nongaap_reconciliations: list[NonGaapReconciliation] = Field(default_factory=list)
    guidance_passages: list[GuidancePassage] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class DualPeriodEarningsRelease(_Frozen):
    entity_id: str
    cik: str
    current: EarningsReleasePeriod | None = None
    prior: EarningsReleasePeriod | None = None
    pairing_mode: Literal["sequential", "yoy"] = "sequential"
    days_between_filings: int | None = None
    gaps: list[str] = Field(default_factory=list)


class FilingReportEntry(_Frozen):
    r_file: str
    short_name: str
    long_name: str
    menu_category: str
    normalized_category: Literal["Statements", "Notes", "Tables", "Cover", "Details", "Other"]
    position: int | None = None
    role: str | None = None
    parent_role: str | None = None
    report_type: str | None = None
    url: str


class FilingTableCatalog(_Frozen):
    entity_id: str
    cik: str
    accession: str
    filing_summary_url: str
    report_count: int
    statements: list[FilingReportEntry] = Field(default_factory=list)
    notes: list[FilingReportEntry] = Field(default_factory=list)
    tables: list[FilingReportEntry] = Field(default_factory=list)
    other: list[FilingReportEntry] = Field(default_factory=list)
    all_reports: list[FilingReportEntry] = Field(default_factory=list)

    def find(self, query: str, *, category: str | None = None) -> list[FilingReportEntry]:
        q = query.strip().casefold()
        if not q:
            return []
        pool = self.all_reports
        if category is not None:
            cat = category.strip().casefold()
            pool = [r for r in pool if r.normalized_category.casefold() == cat or r.menu_category.casefold() == cat]
        return [r for r in pool if q in r.short_name.casefold() or q in r.long_name.casefold()]


class TableColumnMeta(_Frozen):
    index: int
    header: str
    period_label: str | None = None
    period_end: date | None = None
    period_kind: str | None = None
    unit: str | None = None
    scale: str | None = None
    scale_multiplier: int | None = None
    currency: str | None = None


class FilingTableCell(_Frozen):
    column_index: int
    column_header: str
    raw_text: str
    numeric_value: Decimal | None = None
    is_percent: bool = False
    currency: str | None = None
    scale: str | None = None
    period: str | None = None


class FilingTableRow(_Frozen):
    row_index: int
    label: str
    indent_level: int = 0
    is_header: bool = False
    is_abstract: bool = False
    is_total: bool = False
    xbrl_tag: str | None = None
    cells: list[FilingTableCell] = Field(default_factory=list)
    values_by_column: dict[str, str] = Field(default_factory=dict)


class FilingTableData(_Frozen):
    r_file: str
    title: str
    url: str
    table_index: int = 0
    entity_id: str | None = None
    accession: str | None = None
    currency: str | None = None
    unit_scale: str | None = None
    scale_multiplier: int | None = None
    unit_header_note: str | None = None
    periods: list[str] = Field(default_factory=list)
    columns: list[TableColumnMeta] = Field(default_factory=list)
    rows: list[FilingTableRow] = Field(default_factory=list)
    narrative_text: str = ""
    subtables: list["FilingTableData"] = Field(default_factory=list)
    content_sha256: str
    provenance: Provenance | None = None


FilingTableData.model_rebuild()


class FilingSection(_Frozen):
    section_key: str
    item_number: str | None = None
    part: str | None = None
    title: str
    text: str
    paragraphs: list[str] = Field(default_factory=list)
    char_start: int
    char_end: int
    char_count: int
    word_count: int
    content_sha256: str


class FilingSectionsBundle(_Frozen):
    form: str | None = None
    sections: dict[str, FilingSection] = Field(default_factory=dict)
    section_order: list[str] = Field(default_factory=list)
    clean_text: str
    content_sha256: str
    provenance: Provenance | None = None


class RedlineParagraphDiff(_Frozen):
    change_type: Literal["added", "removed", "revised"]
    section_key: str | None = None
    current_index: int | None = None
    prior_index: int | None = None
    current_text: str | None = None
    prior_text: str | None = None
    similarity: float | None = None
    redline_markup: str | None = None


class SectionRedlineDiff(_Frozen):
    section_key: str | None = None
    added: list[RedlineParagraphDiff] = Field(default_factory=list)
    removed: list[RedlineParagraphDiff] = Field(default_factory=list)
    revised: list[RedlineParagraphDiff] = Field(default_factory=list)
    unchanged_count: int = 0
    filtered_noise_count: int = 0
    filtered_noise_examples: list[str] = Field(default_factory=list)
    has_substantive_changes: bool = False


class FilingRedlineReport(_Frozen):
    section_diffs: dict[str, SectionRedlineDiff] = Field(default_factory=dict)
    total_added: int = 0
    total_removed: int = 0
    total_revised: int = 0
    total_filtered_noise: int = 0


# --------------------------------------------------------------------------- Validation & HTTP helpers
def validate_single_cik(cik: str | int | None) -> str:
    """Strictly validate and return a single 10-digit nonzero CIK string."""
    if cik is None or isinstance(cik, bool):
        raise ValueError("CIK is required and must be a single 1 to 10 digit nonzero identifier.")
    if isinstance(cik, (list, tuple, set, dict)):
        raise ValueError("Only a single CIK is allowed.")
    raw = str(cik).strip()
    if not raw or any(sep in raw for sep in (",", ";", "|", "\n", "\r", "\t")) or " " in raw:
        raise ValueError("CIK must be a single 1 to 10 digit identifier (multiple CIKs are not permitted).")
    return cik10(raw)


def normalize_accession(accession: str) -> tuple[str, str]:
    """Return ``(dashed_accession, nodash_accession)`` or raise ``ValueError``."""
    raw = (accession or "").strip()
    match = _ACCESSION_RE.fullmatch(raw)
    if not match:
        raise ValueError("Accession must match ##########-##-###### (18 digits).")
    p1, p2, p3 = match.groups()
    return f"{p1}-{p2}-{p3}", f"{p1}{p2}{p3}"


def validate_r_file(r_file: str) -> str:
    raw = (r_file or "").strip()
    if not _R_FILE_RE.fullmatch(raw):
        raise ValueError("r_file must be a relative report filename like R2.htm.")
    return raw


def _validate_sec_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or (parts.hostname or "").lower() not in SEC_ALLOWED_HOSTS:
        raise ValueError(f"Only official SEC HTTPS hosts are permitted: {url}")
    return url


def _decode_bytes(body: bytes, content_type: str = "") -> str:
    candidates: list[str] = []
    match = _CHARSET_HEADER_RE.search(content_type or "")
    if match:
        candidates.append(match.group(1))
    head = body[:8192]
    for pattern in (_XML_ENCODING_RE, _META_CHARSET_RE):
        m = pattern.search(head)
        if m:
            try:
                candidates.append(m.group(1).decode("ascii"))
            except UnicodeDecodeError:
                pass
    for enc in (*candidates, "utf-8", "cp1252", "latin-1"):
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("latin-1", errors="replace")


def _fetch_bytes(
    adapter: SecEdgarAdapter,
    url: str,
    category: str,
    *,
    accept: str,
    not_found_reason: str = "not_found",
    large: bool = False,
    max_bytes: int = MAX_BYTES,
) -> tuple[bytes, str, Provenance, Cost, Permissions]:
    try:
        _validate_sec_url(url)
    except ValueError as err:
        raise StopWith(ToolResult.failure("failed", "invalid_request", str(err))) from None
    gate = adapter._gate(category)
    if isinstance(gate, ToolResult):
        raise StopWith(gate)
    decision = adapter._limiter.acquire(PROVIDER, 1)
    if not decision.allowed:
        raise StopWith(rate_limited_result(decision, PROVIDER))
    cost = Cost(provider_calls=1)
    headers = {"User-Agent": adapter._ua or "", "Accept": accept}
    stream = adapter._stream if (large and adapter._stream is not None) else None
    try:
        if stream is not None:
            import tempfile

            with tempfile.TemporaryFile() as sink:
                capped = _CappedSink(sink, max_bytes)
                reply = stream("GET", url, headers, b"", adapter._timeout, capped)
                sink.flush()
                sink.seek(0)
                body = sink.read()
        else:
            reply = adapter._transport("GET", url, headers, b"", adapter._timeout)
            body = bytes(reply.body or b"")
            if len(body) > max_bytes:
                raise _TransportFailure("response too large")
    except _TransportTimeout:
        raise StopWith(
            ToolResult.failure("failed", "source_timeout", f"{PROVIDER} request timed out.", retryable=True, cost=cost)
        ) from None
    except (_TransportFailure, OSError) as error:
        if str(error) == "response too large":
            raise StopWith(
                ToolResult.failure("failed", "source_too_large", f"{PROVIDER} response exceeded size limit.", cost=cost)
            ) from None
        raise StopWith(
            ToolResult.failure("failed", "source_unavailable", f"{PROVIDER} request failed.", retryable=True, cost=cost)
        ) from None

    code = reply.status_code
    if code == 429:
        wait = retry_after(reply.headers) or 60.0
        raise StopWith(
            ToolResult.failure(
                "rate_limited",
                "provider_rate_limited",
                f"{PROVIDER} returned HTTP 429.",
                retryable=True,
                cost=cost,
                suggested_actions=[SuggestedAction(action="wait", retry_after_seconds=wait)],
            )
        )
    if code == 404:
        raise StopWith(ToolResult.failure("empty", not_found_reason, f"{PROVIDER} returned HTTP 404.", cost=cost))
    if code in (401, 403):
        raise StopWith(ToolResult.failure("failed", "forbidden", f"{PROVIDER} refused request (HTTP {code}).", cost=cost))
    if not 200 <= code < 300:
        raise StopWith(
            ToolResult.failure(
                "failed",
                "source_unavailable",
                f"{PROVIDER} returned HTTP {code}.",
                retryable=code >= 500,
                cost=cost,
            )
        )

    prov = Provenance(
        provider=PROVIDER,
        endpoint=url,
        retrieved_at=adapter._now(),
        response_sha256=sha256(body).hexdigest(),
    )
    content_type = http_header(reply.headers, "content-type")
    return body, content_type, prov, cost, gate


def _combine_provenance(
    adapter: SecEdgarAdapter,
    provenances: Sequence[Provenance],
    default_endpoint: str,
) -> Provenance:
    if not provenances:
        return Provenance(provider=PROVIDER, endpoint=default_endpoint, retrieved_at=adapter._now())
    hashes = sorted(p.response_sha256 for p in provenances if p.response_sha256)
    endpoints = [p.endpoint for p in provenances]
    digest = sha256("\n".join(hashes).encode("utf-8")).hexdigest() if hashes else None
    return Provenance(
        provider=PROVIDER,
        endpoint=endpoints[0],
        retrieved_at=adapter._now(),
        response_sha256=digest,
        endpoints=endpoints,
    )


# --------------------------------------------------------------------------- HTML cleaning & table grid parser
def _clean_text(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "").replace("\u2009", " ")
    cleaned = cleaned.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    cleaned = re.sub(r"[ \t\r\f\v]+", " ", cleaned)
    return cleaned.strip()


def _clean_cell_text(text: str) -> str:
    cleaned = _clean_text(text)
    if cleaned in ("\u00a0", "&nbsp;"):
        return ""
    return cleaned


@dataclass
class _RawCell:
    text_parts: list[str]
    is_header: bool = False
    colspan: int = 1
    rowspan: int = 1
    indent_level: int = 0
    row_class: str = ""
    xbrl_tag: str | None = None
    subtables: list["_RawTable"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.subtables is None:
            self.subtables = []


@dataclass
class _RawRow:
    cells: list[_RawCell]
    row_class: str = ""


@dataclass
class _RawTable:
    rows: list[_RawRow]
    table_class: str = ""
    table_id: str = ""
    heading_context: str | None = None
    active_row: _RawRow | None = None
    active_cell: _RawCell | None = None


@dataclass
class _GridCell:
    text: str
    is_header: bool = False
    is_span_continuation: bool = False
    indent_level: int = 0
    xbrl_tag: str | None = None
    row_class: str = ""


def _parse_span(value: str | None) -> int:
    if not value:
        return 1
    v = value.strip()
    if v.isdigit():
        num = int(v)
        if 1 <= num <= 64:
            return num
    return 1


def _extract_indent(attrs_map: dict[str, str]) -> int:
    style = attrs_map.get("style", "")
    m = re.search(r"padding-left\s*:\s*(\d+(?:\.\d+)?)\s*(px|pt|em|rem)?", style, re.IGNORECASE)
    if m:
        amt = float(m.group(1))
        unit = (m.group(2) or "px").lower()
        if unit in ("em", "rem"):
            return min(6, max(0, round(amt)))
        if unit == "pt":
            return min(6, max(0, round(amt / 10.0)))
        return min(6, max(0, round(amt / 12.0)))
    return 0


class _StructuredHtmlParser(HTMLParser):
    """HTML parser separating visible prose blocks and structured (possibly nested) tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._tag_stack: list[tuple[str, bool]] = []
        self._table_stack: list[_RawTable] = []
        self._text_chunks: list[str] = []
        self._blocks: list[tuple[Literal["text", "table"], Any]] = []
        self._last_heading: str | None = None
        self._in_heading = False
        self._heading_parts: list[str] = []

    def _flush_text(self) -> None:
        raw = "".join(self._text_chunks)
        self._text_chunks.clear()
        if not raw.strip():
            return
        for line in raw.splitlines():
            cleaned = _clean_text(line)
            if not cleaned:
                continue
            cleaned = re.sub(r"^•\s*[•\-*]\s+", "• ", cleaned)
            if cleaned == "•":
                continue
            self._blocks.append(("text", cleaned))
            if len(cleaned) <= 140 and not cleaned.endswith(".") and not cleaned.startswith(("•", "-", "*")):
                self._last_heading = cleaned

    def _is_hidden_attrs(self, tag: str, attrs_map: dict[str, str]) -> bool:
        if tag in _HARD_HIDDEN_TAGS:
            return True
        style = attrs_map.get("style", "")
        if style and _HIDDEN_STYLE_RE.search(style):
            return True
        cls = {c.lower() for c in attrs_map.get("class", "").split()}
        if "authrefdata" in cls or "hide" in cls:
            return True
        if attrs_map.get("id", "").lower() == "authrefdata":
            return True
        return False

    def _current_cell(self) -> _RawCell | None:
        if not self._table_stack:
            return None
        return self._table_stack[-1].active_cell

    @staticmethod
    def _finish_cell(tbl: _RawTable) -> None:
        if tbl.active_cell is None:
            return
        cell = tbl.active_cell
        tbl.active_cell = None
        if tbl.active_row is None:
            tbl.active_row = _RawRow(cells=[])
        tbl.active_row.cells.append(cell)

    @classmethod
    def _finish_row(cls, tbl: _RawTable) -> None:
        cls._finish_cell(tbl)
        if tbl.active_row is None:
            return
        tbl.rows.append(tbl.active_row)
        tbl.active_row = None

    def _finish_table(self) -> None:
        if not self._table_stack:
            return
        tbl = self._table_stack.pop()
        self._finish_row(tbl)
        parent_cell = self._current_cell()
        if parent_cell is not None:
            parent_cell.subtables.append(tbl)
        else:
            self._blocks.append(("table", tbl))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        attrs_map = {k.lower(): (v or "") for k, v in attrs if k}
        is_hidden = self._is_hidden_attrs(t, attrs_map)
        if t not in _VOID_TAGS:
            self._tag_stack.append((t, is_hidden))
        if is_hidden:
            self._hidden_depth += 1
            return
        if self._hidden_depth > 0:
            return

        if t == "sup":
            cur_cell = self._current_cell()
            if cur_cell is not None:
                cur_cell.text_parts.append(" ")
            return

        if t in _HEADING_TAGS and not self._table_stack:
            self._in_heading = True
            self._heading_parts.clear()

        if t == "table":
            if not self._table_stack:
                self._flush_text()
            raw_table = _RawTable(
                rows=[],
                table_class=attrs_map.get("class", ""),
                table_id=attrs_map.get("id", ""),
                heading_context=self._last_heading,
            )
            self._table_stack.append(raw_table)
            return

        if t == "tr" and self._table_stack:
            cur_tbl = self._table_stack[-1]
            self._finish_row(cur_tbl)
            cur_tbl.active_row = _RawRow(cells=[], row_class=attrs_map.get("class", ""))
            return

        if t in ("td", "th") and self._table_stack:
            cur_tbl = self._table_stack[-1]
            if cur_tbl.active_row is None:
                cur_tbl.active_row = _RawRow(cells=[])
            self._finish_cell(cur_tbl)
            cur_tbl.active_cell = _RawCell(
                text_parts=[],
                is_header=(t == "th"),
                colspan=_parse_span(attrs_map.get("colspan")),
                rowspan=_parse_span(attrs_map.get("rowspan")),
                indent_level=_extract_indent(attrs_map),
                row_class=cur_tbl.active_row.row_class,
            )
            return

        if t == "a":
            cur_cell = self._current_cell()
            if cur_cell is not None:
                onclick = attrs_map.get("onclick", "")
                m = _SHOW_AR_RE.search(onclick)
                if m and cur_cell.xbrl_tag is None:
                    cur_cell.xbrl_tag = m.group(1).removeprefix("defref_")
            return

        if t == "br":
            cur_cell = self._current_cell()
            if cur_cell is not None:
                cur_cell.text_parts.append("\n")
            elif not self._table_stack:
                self._text_chunks.append("\n")
            return

        if t == "li":
            cur_cell = self._current_cell()
            if cur_cell is not None:
                cur_cell.text_parts.append("\n• ")
            elif not self._table_stack:
                self._text_chunks.append("\n• ")
            return

        if t in _BLOCK_TAGS:
            cur_cell = self._current_cell()
            if cur_cell is not None:
                if cur_cell.text_parts and not cur_cell.text_parts[-1].endswith("\n"):
                    cur_cell.text_parts.append("\n")
            elif not self._table_stack:
                self._text_chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        popped_hidden = False
        for idx in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[idx][0] == t:
                removed = self._tag_stack[idx:]
                del self._tag_stack[idx:]
                for _, was_hidden in removed:
                    if was_hidden:
                        self._hidden_depth = max(0, self._hidden_depth - 1)
                        popped_hidden = True
                break
        if popped_hidden or self._hidden_depth > 0 or t in _HARD_HIDDEN_TAGS:
            return

        if t in _HEADING_TAGS and self._in_heading:
            heading = _clean_text("".join(self._heading_parts))
            self._in_heading = False
            self._heading_parts.clear()
            if heading:
                self._last_heading = heading

        if t in ("td", "th") and self._table_stack:
            self._finish_cell(self._table_stack[-1])
            return
        if t == "tr" and self._table_stack:
            self._finish_row(self._table_stack[-1])
            return
        if t == "table" and self._table_stack:
            self._finish_table()
            return
        if t in _BLOCK_TAGS:
            cur_cell = self._current_cell()
            if cur_cell is not None:
                if cur_cell.text_parts and not cur_cell.text_parts[-1].endswith("\n"):
                    cur_cell.text_parts.append("\n")
            elif not self._table_stack:
                self._text_chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._hidden_depth > 0:
            return
        # Collapse raw HTML source newlines/tabs inside text nodes so wrapped <p> text stays one paragraph
        collapsed = re.sub(r"\s+", " ", data)
        if self._in_heading:
            self._heading_parts.append(collapsed)
        cur_cell = self._current_cell()
        if cur_cell is not None:
            cur_cell.text_parts.append(collapsed)
        elif not self._table_stack:
            self._text_chunks.append(collapsed)

    def close(self) -> None:
        super().close()
        while self._table_stack:
            self._finish_table()
        self._flush_text()


def _expand_table_grid(raw_table: _RawTable) -> list[list[_GridCell]]:
    """Expand ``rowspan`` and ``colspan`` into an aligned 2D grid."""
    grid: list[list[_GridCell | None]] = []
    for r_idx, raw_row in enumerate(raw_table.rows):
        while len(grid) <= r_idx:
            grid.append([])
        c_idx = 0
        for raw_cell in raw_row.cells:
            while c_idx < len(grid[r_idx]) and grid[r_idx][c_idx] is not None:
                c_idx += 1
            text = _clean_cell_text(" ".join(raw_cell.text_parts).replace("\n", " "))
            for dr in range(raw_cell.rowspan):
                target_r = r_idx + dr
                while len(grid) <= target_r:
                    grid.append([])
                needed_len = c_idx + raw_cell.colspan
                if len(grid[target_r]) < needed_len:
                    grid[target_r].extend([None] * (needed_len - len(grid[target_r])))
                for dc in range(raw_cell.colspan):
                    is_cont = dr > 0 or dc > 0
                    grid[target_r][c_idx + dc] = _GridCell(
                        text=text if not is_cont else (text if raw_cell.is_header else ""),
                        is_header=raw_cell.is_header,
                        is_span_continuation=is_cont,
                        indent_level=raw_cell.indent_level if not is_cont else 0,
                        xbrl_tag=raw_cell.xbrl_tag if not is_cont else None,
                        row_class=raw_cell.row_class,
                    )
            c_idx += raw_cell.colspan

    if not grid:
        return []
    width = max(len(row) for row in grid)
    normalized: list[list[_GridCell]] = []
    for row in grid:
        padded: list[_GridCell] = []
        for idx in range(width):
            cell = row[idx] if idx < len(row) and row[idx] is not None else _GridCell(text="")
            padded.append(cell)
        normalized.append(padded)
    return normalized


def _merge_currency_and_prune_spacers(grid: list[list[_GridCell]]) -> list[list[_GridCell]]:
    """Merge split ``$``/``)``/``%`` cells and drop SEC spacer columns without misaligning headers."""
    if not grid:
        return []
    # Work on mutable copies
    rows = [[_GridCell(**c.__dict__) for c in row] for row in grid]
    num_rows = len(rows)
    num_cols = len(rows[0])

    # Identify header row boundary (leading rows with <th> or no numeric data when table has multiple rows)
    header_end = 0
    while header_end < num_rows and any(c.is_header for c in rows[header_end]):
        header_end += 1

    # Merge split currency prefixes ("$" + "1,234") and trailing closing/percent markers ("(1,234" + ")")
    for r in range(header_end, num_rows):
        row = rows[r]
        for c in range(num_cols - 1):
            cur = row[c].text.strip()
            if cur in ("$", "US$", "€", "£", "¥"):
                # Find next non-empty cell within 2 columns
                nxt_idx = c + 1 if row[c + 1].text.strip() else (c + 2 if c + 2 < num_cols and row[c + 2].text.strip() else None)
                if nxt_idx is not None:
                    nxt = row[nxt_idx].text.strip()
                    if re.match(r"^[\(\-\d—–]", nxt):
                        row[nxt_idx].text = f"{cur}{nxt}" if cur in ("$", "€", "£", "¥") else f"{cur} {nxt}"
                        row[c].text = ""
        for c in range(1, num_cols):
            cur = row[c].text.strip()
            if cur in (")", "%"):
                prev_idx = c - 1 if row[c - 1].text.strip() else (c - 2 if c >= 2 and row[c - 2].text.strip() else None)
                if prev_idx is not None:
                    prev = row[prev_idx].text.strip()
                    if re.search(r"\d$", prev):
                        row[prev_idx].text = f"{prev}{cur}"
                        row[c].text = ""

    # Determine which columns carry real content (not empty & not solely a colspan continuation)
    keep_cols: list[int] = []
    for c in range(num_cols):
        has_data = any(rows[r][c].text.strip() and not rows[r][c].is_span_continuation for r in range(header_end, num_rows))
        has_header_origin = any(rows[r][c].text.strip() and not rows[r][c].is_span_continuation for r in range(0, header_end))
        # If a column is empty in all data rows, check if its header origin belongs to a colspan that spans a kept column
        if not has_data and has_header_origin and header_end > 0 and num_rows > header_end:
            # Check if column c is just the currency/prefix col inside a colspan header
            transferred = False
            for r in range(0, header_end):
                if rows[r][c].text.strip() and not rows[r][c].is_span_continuation:
                    if c + 1 < num_cols and rows[r][c + 1].is_span_continuation and rows[r][c + 1].text == rows[r][c].text:
                        rows[r][c + 1].is_span_continuation = False
                        transferred = True
            if transferred:
                continue
        if has_data or has_header_origin:
            keep_cols.append(c)

    if not keep_cols:
        keep_cols = [c for c in range(num_cols) if any(rows[r][c].text.strip() for r in range(num_rows))]
    if not keep_cols:
        return []

    pruned: list[list[_GridCell]] = []
    for row in rows:
        new_row = [row[c] for c in keep_cols]
        if any(cell.text.strip() for cell in new_row):
            pruned.append(new_row)
    return pruned


def _looks_like_layout_table(grid: list[list[_GridCell]]) -> bool:
    if not grid:
        return True
    num_cols = max(len(r) for r in grid)
    if num_cols <= 1:
        has_header = any(any(c.is_header for c in r) for r in grid)
        if not has_header:
            return True
    return False


def _format_table_markdown(headers: list[str], rows: list[list[str]]) -> str:
    if not headers and not rows:
        return ""
    lines: list[str] = []
    if headers:
        lines.append("| " + " | ".join(h or "" for h in headers) + " |")
        lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(c or "" for c in row) + " |")
    return "\n".join(lines)


def _table_grid_to_parsed(
    grid: list[list[_GridCell]],
    *,
    title: str | None = None,
) -> ParsedHtmlTable | None:
    if not grid:
        return None
    header_rows = 0
    while header_rows < len(grid) and any(c.is_header for c in grid[header_rows]):
        header_rows += 1
    if header_rows == 0 and len(grid) > 1:
        # Infer header if row 0 has text and no pure numeric data while row 1+ has numbers
        row0_has_num = any(re.fullmatch(r"[\$\(]?\d[\d,\.\)%]*", c.text.strip()) for c in grid[0][1:])
        later_has_num = any(re.search(r"\d", c.text) for r in grid[1:] for c in r[1:])
        if not row0_has_num and later_has_num:
            header_rows = 1

    width = len(grid[0])
    headers: list[str] = []
    if header_rows > 0:
        for c in range(width):
            parts: list[str] = []
            for r in range(header_rows):
                val = grid[r][c].text.strip()
                if val and (not parts or parts[-1] != val):
                    parts.append(val)
            headers.append(" - ".join(parts))
        body_rows = [[c.text for c in row] for row in grid[header_rows:]]
    else:
        headers = [f"col_{i}" for i in range(width)]
        body_rows = [[c.text for c in row] for row in grid]

    flat_text = " ".join([title or "", *headers, *(cell for row in body_rows for cell in row)]).lower()
    is_nongaap = any(
        kw in flat_text
        for kw in (
            "non-gaap",
            "non gaap",
            "reconciliation",
            "adjusted ebitda",
            "adjusted net income",
            "adjusted operating",
            "adjusted diluted",
            "adjusted eps",
            "free cash flow",
        )
    )
    md = _format_table_markdown(headers, body_rows)
    return ParsedHtmlTable(
        title=title,
        headers=headers,
        rows=body_rows,
        raw_markdown=md,
        is_nongaap=is_nongaap,
    )


def strip_html_preserving_tables(html: str) -> tuple[str, list[ParsedHtmlTable]]:
    """Strip HTML cleanly, preserving table structure as pipe tables and parsed table records."""
    parser = _StructuredHtmlParser()
    parser.feed(html or "")
    parser.close()

    out_lines: list[str] = []
    tables: list[ParsedHtmlTable] = []

    def emit_raw_table(raw_table: _RawTable) -> None:
        # Handle embedded subtables inside Note-like single-cell tables first
        embedded_subtables: list[_RawTable] = []
        for row in raw_table.rows:
            for cell in row.cells:
                if cell.subtables:
                    embedded_subtables.extend(cell.subtables)

        grid = _merge_currency_and_prune_spacers(_expand_table_grid(raw_table))
        if _looks_like_layout_table(grid):
            for row in raw_table.rows:
                for cell in row.cells:
                    txt = _clean_text(" ".join(cell.text_parts))
                    if txt:
                        out_lines.append(txt)
                    for sub in cell.subtables:
                        emit_raw_table(sub)
            return

        parsed = _table_grid_to_parsed(grid, title=raw_table.heading_context)
        if parsed is not None:
            tables.append(parsed)
            if parsed.raw_markdown:
                out_lines.append("")
                out_lines.append(parsed.raw_markdown)
                out_lines.append("")
        for sub in embedded_subtables:
            emit_raw_table(sub)

    for kind, payload in parser._blocks:
        if kind == "text":
            out_lines.append(payload)
        elif kind == "table":
            emit_raw_table(payload)

    # Collapse excess blank lines
    collapsed: list[str] = []
    prev_blank = True
    for line in out_lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        collapsed.append(line.rstrip())
        prev_blank = is_blank
    while collapsed and not collapsed[-1].strip():
        collapsed.pop()
    return "\n".join(collapsed), tables


# --------------------------------------------------------------------------- 1. sec_earnings_release (8-K Item 2.02)
_SAFE_HARBOR_RE = re.compile(
    r"(?:private securities litigation reform act|safe harbor|actual results (?:may|could|might) differ materially|"
    r"forward-looking statements involve risks|undertakes no obligation to update|factors that could cause actual results)",
    re.IGNORECASE,
)
_GUIDANCE_HEADING_RE = re.compile(
    r"(?:^|\b)(?:business\s+outlook|financial\s+outlook|guidance|outlook|expectations|"
    r"(?:first|second|third|fourth)\s+quarter\s+.*\s+outlook|fiscal\s+20\d{2}\s+outlook)\b",
    re.IGNORECASE,
)
_GUIDANCE_TEXT_RE = re.compile(
    r"(?:\bexpects?\b|\bguidance\b|\boutlook\b|\bprojected?\b|\bforecast(?:s|ed)?\b|\banticipates?\b|\btargets?\b|"
    r"is expected to be|are expected to be|expected to range|in the range of|"
    r"for the (?:first|second|third|fourth|upcoming|next)\s+quarter|"
    r"for (?:full[\s\-]?year|fiscal)\s+20\d{2})",
    re.IGNORECASE,
)
_GUIDANCE_METRIC_RE = re.compile(
    r"(?:\$|%|\brevenue\b|\bsales\b|\bgross margin\b|\boperating (?:income|margin|expenses?)\b|"
    r"\bearnings per share\b|\beps\b|\bebitda\b|\bcapex\b|\bcapital expenditures?\b|\bfree cash flow\b|\btax rate\b)",
    re.IGNORECASE,
)
_NONGAAP_HEADING_RE = re.compile(
    r"(?:use of\s+)?non[-\s]?gaap|reconciliation of\s+(?:gaap|net income|operating|diluted|cash flow)|"
    r"adjusted\s+(?:ebitda|eps|net income|operating income)|free cash flow reconciliation",
    re.IGNORECASE,
)
_SUMMARY_STOP_RE = re.compile(
    r"^(?:forward[-\s]?looking statements|safe harbor|conference call|webcast|about\s+|"
    r"condensed consolidated|consolidated statements|use of non[-\s]?gaap|non[-\s]?gaap financial|"
    r"financial outlook|business outlook|guidance)\b",
    re.IGNORECASE,
)


def _detect_metrics(text: str) -> list[str]:
    found: list[str] = []
    checks = (
        ("revenue", r"\b(?:revenue|revenues|net sales)\b"),
        ("gross_margin", r"\bgross\s+(?:margin|profit)\b"),
        ("operating_income", r"\boperating\s+(?:income|profit|margin)\b"),
        ("operating_expenses", r"\boperating\s+expenses?\b|\bopex\b"),
        ("net_income", r"\bnet\s+(?:income|loss|earnings)\b"),
        ("eps", r"\b(?:eps|earnings\s+per\s+(?:diluted\s+)?share)\b"),
        ("adjusted_ebitda", r"\badjusted\s+ebitda\b|\bebitda\b"),
        ("free_cash_flow", r"\bfree\s+cash\s+flow\b|\bfcf\b"),
        ("capex", r"\bcapex\b|\bcapital\s+expenditures?\b"),
    )
    for key, pat in checks:
        if re.search(pat, text, re.IGNORECASE):
            found.append(key)
    return found


def _infer_horizon(text: str) -> str:
    low = text.lower()
    if any(k in low for k in ("full year", "full-year", "fiscal 20", "annual")):
        if any(k in low for k in ("quarter", "q1", "q2", "q3", "q4")):
            return "next_quarter_and_full_year"
        return "full_year"
    if any(k in low for k in ("first quarter", "second quarter", "third quarter", "fourth quarter", "next quarter", "q1", "q2", "q3", "q4")):
        return "next_quarter"
    return "unspecified"


def _extract_release_hints(
    text: str,
    tables: Sequence[ParsedHtmlTable],
    *,
    source_exhibit: str = "EX-99.1",
) -> tuple[str, list[str], list[NonGaapReconciliation], list[GuidancePassage]]:
    """Extract management summary/highlights, Non-GAAP reconciliations, and forward guidance passages."""
    lines = [line.strip() for line in text.splitlines()]
    # Split into logical sections/blocks
    blocks: list[tuple[str | None, str]] = []
    current_heading: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if buf:
            blocks.append((current_heading, "\n".join(buf).strip()))
            buf.clear()

    for line in lines:
        if not line:
            flush()
            continue
        if _SAFE_HARBOR_RE.search(line):
            flush()
            blocks.append((current_heading, line))
            continue
        is_heading = (
            not line.startswith("|")
            and len(line) <= 110
            and not line.endswith(".")
            and (
                _GUIDANCE_HEADING_RE.search(line)
                or _NONGAAP_HEADING_RE.search(line)
                or _SUMMARY_STOP_RE.search(line)
                or line.isupper()
                or re.match(r"^(?:Q[1-4]|First|Second|Third|Fourth|Full|Fiscal|Summary|Financial|Operational|Recent|Key)\b", line)
            )
        )
        if is_heading:
            flush()
            current_heading = line
        else:
            if line.startswith("|") and buf and not buf[-1].startswith("|"):
                flush()
            elif not line.startswith("|") and buf and buf[-1].startswith("|"):
                flush()
            buf.append(line)
    flush()

    # 1. Management summary & highlights: opening prose/quotes/bullets before financial statements/boilerplate
    summary_parts: list[str] = []
    highlights: list[str] = []
    for heading, body in blocks:
        if heading and _SUMMARY_STOP_RE.search(heading):
            break
        if body.startswith("|") and len(summary_parts) >= 1:
            continue
        if _SAFE_HARBOR_RE.search(body):
            if summary_parts:
                break
            continue
        if heading:
            summary_parts.append(f"{heading}: {body}")
        else:
            summary_parts.append(body)
        for line in body.splitlines():
            stripped = line.lstrip("•-* ").strip()
            if line.startswith(("•", "-", "*")) and stripped:
                highlights.append(stripped)
            elif ('"' in line or "said" in line.lower() or "commented" in line.lower()) and len(stripped) > 40:
                highlights.append(stripped)
        if sum(len(p) for p in summary_parts) >= 2500:
            break

    management_summary = "\n\n".join(summary_parts[:8]).strip()

    # 2. Non-GAAP reconciliations: match headings/blocks + tables flagged is_nongaap
    nongaap_items: list[NonGaapReconciliation] = []
    seen_table_md: set[str] = set()
    for heading, body in blocks:
        if _SAFE_HARBOR_RE.search(body):
            continue
        if (heading and _NONGAAP_HEADING_RE.search(heading)) or (
            re.search(r"\bnon[-\s]?gaap\b", body, re.IGNORECASE) and re.search(r"\breconcili", body, re.IGNORECASE)
        ):
            nongaap_items.append(
                NonGaapReconciliation(
                    title=heading,
                    text=body,
                    table=None,
                    metrics_mentioned=_detect_metrics(f"{heading or ''} {body}"),
                    source_exhibit=source_exhibit,
                )
            )
    for tbl in tables:
        if tbl.is_nongaap and tbl.raw_markdown not in seen_table_md:
            seen_table_md.add(tbl.raw_markdown)
            tbl_text = f"{tbl.title or ''} {tbl.raw_markdown}"
            nongaap_items.append(
                NonGaapReconciliation(
                    title=tbl.title,
                    text=tbl.raw_markdown,
                    table=tbl,
                    metrics_mentioned=_detect_metrics(tbl_text),
                    source_exhibit=source_exhibit,
                )
            )

    # 3. Forward guidance passages (strictly excluding Safe Harbor boilerplate)
    guidance_items: list[GuidancePassage] = []
    seen_guidance: set[str] = set()
    for heading, body in blocks:
        if _SAFE_HARBOR_RE.search(body):
            continue
        if heading and re.search(r"forward[-\s]?looking statements|safe harbor", heading, re.IGNORECASE):
            continue
        heading_hit = bool(heading and _GUIDANCE_HEADING_RE.search(heading))
        text_hit = bool(_GUIDANCE_TEXT_RE.search(body) and _GUIDANCE_METRIC_RE.search(body))
        if heading_hit or text_hit:
            passage_body = body
            if not heading_hit and len(body.splitlines()) > 2:
                matched_lines = [
                    ln for ln in body.splitlines()
                    if _GUIDANCE_TEXT_RE.search(ln) and _GUIDANCE_METRIC_RE.search(ln) and not _SAFE_HARBOR_RE.search(ln)
                ]
                if matched_lines:
                    passage_body = "\n".join(matched_lines)
            norm = re.sub(r"\s+", " ", passage_body).strip()
            if norm and norm not in seen_guidance:
                seen_guidance.add(norm)
                guidance_items.append(
                    GuidancePassage(
                        heading=heading,
                        text=passage_body,
                        horizon=_infer_horizon(f"{heading or ''} {passage_body}"),
                        metrics_mentioned=_detect_metrics(f"{heading or ''} {passage_body}"),
                        source_exhibit=source_exhibit,
                    )
                )

    return management_summary, highlights[:12], nongaap_items, guidance_items


@dataclass
class _EightKCandidate:
    accession: str
    nodash: str
    form: str
    filed_at: date
    accepted_at: datetime | None
    period_of_report: date | None
    primary_document: str | None
    primary_document_url: str | None
    index_url: str | None
    description: str | None
    items: list[str] | None


def _normalize_items(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        out = [str(x).strip().upper().removeprefix("ITEM").strip() for x in raw if str(x).strip()]
        return out
    if isinstance(raw, str):
        parts = [p.strip().upper().removeprefix("ITEM").strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]
        return parts
    return None


def _parse_date(val: Any) -> date | None:
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            return None
    return None


def _parse_dt(val: Any) -> datetime | None:
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str) and val.strip():
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _extract_candidates_from_raw_submissions(cik: str, doc: Mapping[str, Any]) -> list[_EightKCandidate]:
    recent = (doc.get("filings") or {}).get("recent") if isinstance(doc.get("filings"), Mapping) else doc
    if not isinstance(recent, Mapping):
        return []
    accs = recent.get("accessionNumber")
    if not isinstance(accs, list):
        return []
    has_items = isinstance(recent.get("items"), list)

    def col(name: str, i: int) -> Any:
        arr = recent.get(name)
        return arr[i] if isinstance(arr, list) and i < len(arr) else None

    out: list[_EightKCandidate] = []
    for i, acc in enumerate(accs):
        form = col("form", i)
        filed = _parse_date(col("filingDate", i))
        if not isinstance(acc, str) or not isinstance(form, str) or filed is None:
            continue
        if form.upper().removesuffix("/A") != "8-K":
            continue
        try:
            dashed, nodash = normalize_accession(acc)
        except ValueError:
            continue
        primary = col("primaryDocument", i)
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
        items = _normalize_items(col("items", i)) if has_items else None
        out.append(
            _EightKCandidate(
                accession=dashed,
                nodash=nodash,
                form=form.upper(),
                filed_at=filed,
                accepted_at=_parse_dt(col("acceptanceDateTime", i)),
                period_of_report=_parse_date(col("reportDate", i)),
                primary_document=primary if isinstance(primary, str) and primary else None,
                primary_document_url=f"{base_url}/{primary}" if isinstance(primary, str) and primary else None,
                index_url=f"{base_url}/{dashed}-index.htm",
                description=col("primaryDocDescription", i) if isinstance(col("primaryDocDescription", i), str) else None,
                items=items,
            )
        )
    return out


def _extract_candidates_from_sequence(cik: str, seq: Sequence[Any]) -> list[_EightKCandidate]:
    out: list[_EightKCandidate] = []
    for item in seq:
        if isinstance(item, Filing):
            if item.form.upper().removesuffix("/A") != "8-K":
                continue
            try:
                dashed, nodash = normalize_accession(item.accession)
            except ValueError:
                continue
            primary = item.primary_document_url.rsplit("/", 1)[-1] if item.primary_document_url else None
            out.append(
                _EightKCandidate(
                    accession=dashed,
                    nodash=nodash,
                    form=item.form.upper(),
                    filed_at=item.filed_at,
                    accepted_at=item.accepted_at,
                    period_of_report=item.period_of_report,
                    primary_document=primary,
                    primary_document_url=item.primary_document_url,
                    index_url=item.index_url or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/{dashed}-index.htm",
                    description=item.description,
                    items=None,
                )
            )
        elif isinstance(item, Mapping):
            form = str(item.get("form", "")).upper()
            if form.removesuffix("/A") != "8-K":
                continue
            acc = item.get("accession") or item.get("accessionNumber")
            filed = _parse_date(item.get("filed_at") or item.get("filingDate"))
            if not isinstance(acc, str) or filed is None:
                continue
            try:
                dashed, nodash = normalize_accession(acc)
            except ValueError:
                continue
            base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
            primary_url = item.get("primary_document_url")
            primary_doc = item.get("primaryDocument") or (primary_url.rsplit("/", 1)[-1] if isinstance(primary_url, str) else None)
            if not primary_url and isinstance(primary_doc, str):
                primary_url = f"{base_url}/{primary_doc}"
            out.append(
                _EightKCandidate(
                    accession=dashed,
                    nodash=nodash,
                    form=form,
                    filed_at=filed,
                    accepted_at=_parse_dt(item.get("accepted_at") or item.get("acceptanceDateTime")),
                    period_of_report=_parse_date(item.get("period_of_report") or item.get("reportDate")),
                    primary_document=primary_doc if isinstance(primary_doc, str) else None,
                    primary_document_url=primary_url if isinstance(primary_url, str) else None,
                    index_url=item.get("index_url") or f"{base_url}/{dashed}-index.htm",
                    description=item.get("description") or item.get("primaryDocDescription"),
                    items=_normalize_items(item.get("items")),
                )
            )
    return out


class _IndexHtmParser(HTMLParser):
    """Parse SEC ``{accession}-index.htm`` document table and Items box."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.rows: list[list[str]] = []
        self._in_tr = False
        self._in_cell = False
        self._cur_row: list[str] = []
        self._cur_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "tr":
            self._in_tr = True
            self._cur_row = []
        elif t in ("td", "th") and self._in_tr:
            self._in_cell = True
            self._cur_cell = []

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cur_row.append(_clean_text("".join(self._cur_cell)))
        elif t == "tr" and self._in_tr:
            self._in_tr = False
            if self._cur_row:
                self.rows.append(self._cur_row)

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        if self._in_cell:
            self._cur_cell.append(data)


def _parse_index_htm(html: str) -> tuple[list[str] | None, dict[str, tuple[str, str | None]]]:
    """Return ``(items_list_or_None, {'EX-99.1': (filename, desc), 'EX-99.2': ...})``."""
    parser = _IndexHtmParser()
    parser.feed(html or "")
    parser.close()
    full_text = _clean_text(" ".join(parser.text_parts))
    items_found = re.findall(r"\bItem\s+(\d+\.\d{2})\b", full_text, re.IGNORECASE)
    items = [x.upper() for x in dict.fromkeys(items_found)] if items_found else None

    exhibits: dict[str, tuple[str, str | None]] = {}
    for row in parser.rows:
        if len(row) < 4:
            continue
        # Typical columns: Seq | Description | Document | Type | Size
        desc = row[1] if len(row) > 1 else ""
        doc_name = row[2].split()[0] if len(row) > 2 and row[2] else ""
        doc_type = row[3] if len(row) > 3 else ""
        if not doc_name or not _SAFE_FILENAME_RE.fullmatch(doc_name):
            continue
        if _EX99_1_TYPE_RE.fullmatch(doc_type):
            exhibits.setdefault("EX-99.1", (doc_name, desc or None))
        elif _EX99_2_TYPE_RE.fullmatch(doc_type):
            exhibits.setdefault("EX-99.2", (doc_name, desc or None))
    return items, exhibits


def _identify_exhibits_from_index_json(
    doc: Any,
    *,
    primary_document: str | None = None,
) -> tuple[list[str] | None, dict[str, tuple[str, str | None]], str | None]:
    """Inspect ``index.json`` for Item hints, EX-99.1/EX-99.2 entries, and ``*-index.htm(l)``."""
    if not isinstance(doc, Mapping):
        return None, {}, None
    directory = doc.get("directory")
    if not isinstance(directory, Mapping):
        return None, {}, None
    items_meta = _normalize_items(directory.get("items") or doc.get("items"))
    entries = directory.get("item")
    if not isinstance(entries, list):
        return items_meta, {}, None

    exhibits: dict[str, tuple[str, str | None]] = {}
    index_htm: str | None = None
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _SAFE_FILENAME_RE.fullmatch(name):
            continue
        if name.lower().endswith(("-index.htm", "-index.html")) and index_htm is None:
            index_htm = name
            continue
        if primary_document and name == primary_document:
            continue
        entry_type = str(entry.get("type") or entry.get("document_type") or "").strip()
        desc = str(entry.get("description") or "").strip() or None
        if _EX99_1_TYPE_RE.fullmatch(entry_type):
            exhibits["EX-99.1"] = (name, desc)
            continue
        if _EX99_2_TYPE_RE.fullmatch(entry_type):
            exhibits["EX-99.2"] = (name, desc)
            continue
        if not _HTML_EXT_RE.search(name):
            continue
        if _EX99_1_NAME_RE.search(name) or (desc and re.search(r"\b(?:exhibit\s*99\.1|earnings\s+release)\b", desc, re.IGNORECASE)):
            exhibits.setdefault("EX-99.1", (name, desc))
        elif _EX99_2_NAME_RE.search(name) or (desc and re.search(r"\bexhibit\s*99\.2\b", desc, re.IGNORECASE)):
            exhibits.setdefault("EX-99.2", (name, desc))
    return items_meta, exhibits, index_htm


def _fetch_json_via_adapter(
    adapter: SecEdgarAdapter,
    url: str,
    category: str,
    not_found_reason: str = "not_found",
) -> tuple[Any, Provenance, Cost, Permissions]:
    try:
        _validate_sec_url(url)
    except ValueError as err:
        raise StopWith(ToolResult.failure("failed", "invalid_request", str(err))) from None
    return adapter._get(url, category, not_found_reason)


def _same_period_8k(a: _EightKCandidate, b: _EightKCandidate) -> bool:
    if a.period_of_report and b.period_of_report and a.period_of_report == b.period_of_report:
        return True
    if abs((a.filed_at - b.filed_at).days) <= 21:
        return True
    return False


def _pair_dual_period(
    candidates: list[_EightKCandidate],
    *,
    pairing_mode: Literal["sequential", "yoy"],
) -> tuple[_EightKCandidate | None, _EightKCandidate | None]:
    if not candidates:
        return None, None
    # Group same-period amendments/follow-ups, keeping the newest per period bucket
    buckets: list[_EightKCandidate] = []
    for cand in candidates:
        if any(_same_period_8k(cand, existing) for existing in buckets):
            continue
        buckets.append(cand)
    current = buckets[0]
    if len(buckets) < 2:
        return current, None
    if pairing_mode == "sequential":
        return current, buckets[1]
    # YoY: pick filing closest to 365 days earlier within [250, 460] days
    yoy_pool = [b for b in buckets[1:] if 250 <= (current.filed_at - b.filed_at).days <= 460]
    if not yoy_pool:
        return current, None
    prior = min(yoy_pool, key=lambda b: abs((current.filed_at - b.filed_at).days - 365))
    return current, prior


def _resolve_index_exhibits(
    adapter: SecEdgarAdapter,
    target_cik: str,
    cand: _EightKCandidate,
    provs: list[Provenance],
) -> tuple[str, dict[str, tuple[str, str | None]], str | None, list[str] | None, int, Permissions | None]:
    """Fetch 8-K directory ``index.json`` and fall back to ``*-index.htm`` / primary doc only when needed."""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{cand.nodash}"
    index_json_url = f"{base_url}/index.json"
    calls = 0
    gate: Permissions | None = None
    exhibits: dict[str, tuple[str, str | None]] = {}
    index_htm: str | None = None
    resolved_items: list[str] | None = cand.items

    try:
        idx_doc, idx_prov, idx_cost, gate = _fetch_json_via_adapter(adapter, index_json_url, "filings", "directory_not_found")
        provs.append(idx_prov)
        calls += idx_cost.provider_calls
        idx_items, exhibits, index_htm = _identify_exhibits_from_index_json(idx_doc, primary_document=cand.primary_document)
        if resolved_items is None and idx_items is not None:
            resolved_items = idx_items
    except StopWith as stop:
        if stop.result.status in ("rate_limited", "denied"):
            raise
        calls += stop.result.cost.provider_calls
        return index_json_url, exhibits, index_htm, resolved_items, calls, gate

    if resolved_items is None and cand.description and "2.02" in cand.description:
        resolved_items = ["2.02"]

    if resolved_items is None and cand.primary_document_url:
        try:
            p_bytes, p_ct, p_prov, p_cost, gate = _fetch_bytes(
                adapter, cand.primary_document_url, "filings", accept="text/html, text/plain", not_found_reason="primary_not_found"
            )
            provs.append(p_prov)
            calls += p_cost.provider_calls
            p_text, _ = strip_html_preserving_tables(_decode_bytes(p_bytes, p_ct))
            found_items = [x.upper() for x in dict.fromkeys(re.findall(r"\bItem\s+(\d+\.\d{2})\b", p_text, re.IGNORECASE))]
            if found_items:
                resolved_items = found_items
        except StopWith as stop:
            if stop.result.status in ("rate_limited", "denied"):
                raise
            calls += stop.result.cost.provider_calls

    if (resolved_items is None or "EX-99.1" not in exhibits) and index_htm:
        try:
            ih_bytes, ih_ct, ih_prov, ih_cost, gate = _fetch_bytes(
                adapter, f"{base_url}/{index_htm}", "filings", accept="text/html", not_found_reason="index_htm_not_found"
            )
            provs.append(ih_prov)
            calls += ih_cost.provider_calls
            htm_items, htm_exhibits = _parse_index_htm(_decode_bytes(ih_bytes, ih_ct))
            if resolved_items is None and htm_items is not None:
                resolved_items = htm_items
            for k, v in htm_exhibits.items():
                exhibits.setdefault(k, v)
        except StopWith as stop:
            if stop.result.status in ("rate_limited", "denied"):
                raise
            calls += stop.result.cost.provider_calls

    return index_json_url, exhibits, index_htm, resolved_items, calls, gate


def sec_earnings_release(
    adapter: SecEdgarAdapter,
    cik: str | int,
    *,
    submissions: Sequence[Filing] | ToolResult | Mapping[str, Any] | None = None,
    as_of: date | None = None,
    pairing_mode: Literal["sequential", "yoy"] = "sequential",
    include_ex99_2: bool = True,
    max_candidates: int = 15,
) -> ToolResult:
    """Fetch dual-period (current & prior quarter) 8-K Item 2.02 EX-99.1 / optional EX-99.2 releases."""
    try:
        target_cik = validate_single_cik(cik)
    except ValueError as err:
        return ToolResult.failure("failed", "invalid_request", str(err))
    if pairing_mode not in ("sequential", "yoy") or max_candidates < 1:
        return ToolResult.failure("failed", "invalid_request", "pairing_mode must be sequential|yoy and max_candidates >= 1.")

    gate = adapter._gate("filings")
    if isinstance(gate, ToolResult):
        return gate

    eid = cik_entity_id(target_cik)
    provs: list[Provenance] = []
    total_calls = 0

    if submissions is None:
        sub_url = f"https://data.sec.gov/submissions/CIK{target_cik}.json"
        try:
            sub_doc, sub_prov, sub_cost, gate = _fetch_json_via_adapter(adapter, sub_url, "filings", "cik_not_found")
        except StopWith as stop:
            return stop.result
        provs.append(sub_prov)
        total_calls += sub_cost.provider_calls
        candidates = _extract_candidates_from_raw_submissions(target_cik, sub_doc)
    elif isinstance(submissions, ToolResult):
        if submissions.status not in ("ok", "partial"):
            return submissions
        candidates = _extract_candidates_from_sequence(target_cik, submissions.data)
    elif isinstance(submissions, Mapping):
        candidates = _extract_candidates_from_raw_submissions(target_cik, submissions)
    else:
        candidates = _extract_candidates_from_sequence(target_cik, submissions)

    if as_of is not None:
        candidates = [c for c in candidates if c.filed_at <= as_of]
    candidates.sort(key=lambda c: (c.filed_at, c.accepted_at or datetime.min.replace(tzinfo=timezone.utc), c.accession), reverse=True)

    if not candidates:
        cov = Coverage(entity_id=eid, requested={"cik": target_cik, "form": "8-K"}, rows=0, data_as_of=adapter._now().date())
        return ToolResult(status="empty", reason="no_8k_filings", coverage=cov, permissions=gate,
                          provenance=_combine_provenance(adapter, provs, f"https://data.sec.gov/submissions/CIK{target_cik}.json"),
                          cost=Cost(provider_calls=total_calls), message="No 8-K filings found for issuer.")

    # Resolve which 8-Ks are Item 2.02 and inspect directory index.json (stopping early once sequential pair is clear)
    prefetched_index: dict[str, tuple[str, dict[str, tuple[str, str | None]], str | None, list[str] | None]] = {}
    item_202_candidates: list[_EightKCandidate] = []
    distinct_buckets: list[_EightKCandidate] = []

    for cand in candidates[:max_candidates]:
        if pairing_mode == "sequential" and len(distinct_buckets) >= 2 and not _same_period_8k(cand, distinct_buckets[1]):
            break
        if cand.items is not None:
            if "2.02" in cand.items:
                item_202_candidates.append(cand)
                if not any(_same_period_8k(cand, b) for b in distinct_buckets):
                    distinct_buckets.append(cand)
            continue

        try:
            idx_url, exhibits, index_htm, resolved_items, idx_calls, idx_gate = _resolve_index_exhibits(
                adapter, target_cik, cand, provs
            )
        except StopWith as stop:
            return stop.result
        total_calls += idx_calls
        if idx_gate is not None:
            gate = idx_gate

        is_202 = ("2.02" in resolved_items) if resolved_items is not None else ("EX-99.1" in exhibits)
        prefetched_index[cand.accession] = (idx_url, exhibits, index_htm, resolved_items or [])
        if is_202:
            resolved_cand = _EightKCandidate(
                accession=cand.accession,
                nodash=cand.nodash,
                form=cand.form,
                filed_at=cand.filed_at,
                accepted_at=cand.accepted_at,
                period_of_report=cand.period_of_report,
                primary_document=cand.primary_document,
                primary_document_url=cand.primary_document_url,
                index_url=cand.index_url,
                description=cand.description,
                items=resolved_items or ["2.02"],
            )
            item_202_candidates.append(resolved_cand)
            if not any(_same_period_8k(resolved_cand, b) for b in distinct_buckets):
                distinct_buckets.append(resolved_cand)

    current_cand, prior_cand = _pair_dual_period(item_202_candidates, pairing_mode=pairing_mode)
    if current_cand is None:
        cov = Coverage(entity_id=eid, requested={"cik": target_cik, "item": "2.02"}, rows=0, data_as_of=adapter._now().date())
        return ToolResult(
            status="empty",
            reason="no_item_202_filings",
            coverage=cov,
            permissions=gate,
            provenance=_combine_provenance(adapter, provs, f"https://data.sec.gov/submissions/CIK{target_cik}.json"),
            cost=Cost(provider_calls=total_calls),
            message="No Item 2.02 earnings 8-K filings were identified.",
        )

    def build_period(role: Literal["current", "prior"], cand: _EightKCandidate) -> EarningsReleasePeriod | ToolResult:
        nonlocal total_calls, gate
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{cand.nodash}"
        index_json_url = f"{base_url}/index.json"
        period_gaps: list[str] = []

        if cand.accession in prefetched_index:
            index_json_url, exhibits, index_htm, resolved_items = prefetched_index[cand.accession]
        else:
            resolved_items = cand.items or ["2.02"]
            exhibits = {}
            index_htm = None
            try:
                idx_doc, idx_prov, idx_cost, gate = _fetch_json_via_adapter(adapter, index_json_url, "filings", "directory_not_found")
                provs.append(idx_prov)
                total_calls += idx_cost.provider_calls
                _, exhibits, index_htm = _identify_exhibits_from_index_json(idx_doc, primary_document=cand.primary_document)
            except StopWith as stop:
                if stop.result.status in ("rate_limited", "denied"):
                    return stop.result
                total_calls += stop.result.cost.provider_calls
                period_gaps.append(f"index_json_unavailable:{stop.result.reason}")

            if "EX-99.1" not in exhibits and index_htm:
                try:
                    ih_bytes, ih_ct, ih_prov, ih_cost, gate = _fetch_bytes(
                        adapter, f"{base_url}/{index_htm}", "filings", accept="text/html", not_found_reason="index_htm_not_found"
                    )
                    provs.append(ih_prov)
                    total_calls += ih_cost.provider_calls
                    _, htm_exhibits = _parse_index_htm(_decode_bytes(ih_bytes, ih_ct))
                    for k, v in htm_exhibits.items():
                        exhibits.setdefault(k, v)
                except StopWith as stop:
                    if stop.result.status in ("rate_limited", "denied"):
                        return stop.result
                    total_calls += stop.result.cost.provider_calls

        def load_exhibit(ex_type: Literal["EX-99.1", "EX-99.2"]) -> EarningsReleaseExhibit | None:
            nonlocal total_calls, gate
            ex_slug = ex_type.lower().replace("-", "").replace(".", "_")
            pair = exhibits.get(ex_type)
            if pair is None:
                if ex_type == "EX-99.1":
                    period_gaps.append("missing_ex99_1")
                return None
            fname, desc = pair
            url = f"{base_url}/{fname}"
            try:
                raw_bytes, ct, ex_prov, ex_cost, gate = _fetch_bytes(
                    adapter,
                    url,
                    "filings",
                    accept="text/html, text/plain",
                    not_found_reason=f"missing_{ex_slug}",
                    max_bytes=MAX_EXHIBIT_BYTES,
                )
            except StopWith as stop:
                if stop.result.status in ("rate_limited", "denied"):
                    raise
                total_calls += stop.result.cost.provider_calls
                period_gaps.append(f"{ex_slug}_fetch_failed:{stop.result.reason}")
                return None
            provs.append(ex_prov)
            total_calls += ex_cost.provider_calls
            decoded = _decode_bytes(raw_bytes, ct)
            cleaned_text, parsed_tables = strip_html_preserving_tables(decoded)
            if not cleaned_text.strip():
                period_gaps.append(f"{ex_slug}_malformed_or_empty")
                return None
            return EarningsReleaseExhibit(
                exhibit_type=ex_type,
                filename=fname,
                url=url,
                description=desc,
                text=cleaned_text,
                tables=parsed_tables,
                content_sha256=sha256(cleaned_text.encode("utf-8")).hexdigest(),
                provenance=ex_prov,
            )

        try:
            ex99_1 = load_exhibit("EX-99.1")
            ex99_2 = load_exhibit("EX-99.2") if include_ex99_2 else None
        except StopWith as stop:
            return stop.result

        summary, highlights, nongaap, guidance = ("", [], [], [])
        if ex99_1 is not None:
            summary, highlights, nongaap, guidance = _extract_release_hints(ex99_1.text, ex99_1.tables, source_exhibit="EX-99.1")
        if ex99_2 is not None:
            s2, h2, ng2, g2 = _extract_release_hints(ex99_2.text, ex99_2.tables, source_exhibit="EX-99.2")
            if not summary:
                summary = s2
            highlights = [*highlights, *h2][:12]
            nongaap = [*nongaap, *ng2]
            guidance = [*guidance, *g2]

        return EarningsReleasePeriod(
            period_role=role,
            accession=cand.accession,
            filed_at=cand.filed_at,
            accepted_at=cand.accepted_at,
            period_of_report=cand.period_of_report,
            form=cand.form,
            items=resolved_items,
            index_json_url=index_json_url,
            ex99_1=ex99_1,
            ex99_2=ex99_2,
            management_summary=summary,
            management_highlights=highlights,
            nongaap_reconciliations=nongaap,
            guidance_passages=guidance,
            gaps=period_gaps,
        )

    current_period = build_period("current", current_cand)
    if isinstance(current_period, ToolResult):
        return current_period
    prior_period: EarningsReleasePeriod | None = None
    if prior_cand is not None:
        built_prior = build_period("prior", prior_cand)
        if isinstance(built_prior, ToolResult):
            return built_prior
        prior_period = built_prior

    overall_gaps: list[str] = []
    if prior_period is None:
        overall_gaps.append("missing_prior_period_8k")
    if current_period.ex99_1 is None:
        overall_gaps.append("current_missing_ex99_1")
    if prior_period is not None and prior_period.ex99_1 is None:
        overall_gaps.append("prior_missing_ex99_1")
    days_between = (current_period.filed_at - prior_period.filed_at).days if prior_period is not None else None
    if days_between is not None and pairing_mode == "sequential" and days_between > 150:
        overall_gaps.append(f"wide_sequential_gap_days:{days_between}")

    has_any_exhibit = any(
        p is not None and (p.ex99_1 is not None or p.ex99_2 is not None)
        for p in (current_period, prior_period)
    )
    if not has_any_exhibit:
        cov = Coverage(
            entity_id=eid,
            requested={"cik": target_cik, "pairing_mode": pairing_mode},
            rows=0,
            data_as_of=adapter._now().date(),
            notes=[*overall_gaps, *current_period.gaps],
        )
        return ToolResult(
            status="empty",
            reason="missing_ex99_1",
            coverage=cov,
            permissions=gate,
            provenance=_combine_provenance(adapter, provs, current_period.index_json_url),
            cost=Cost(provider_calls=total_calls),
            message="Item 2.02 8-K filings were found, but no usable EX-99.1 or EX-99.2 exhibit could be extracted.",
        )

    payload = DualPeriodEarningsRelease(
        entity_id=eid,
        cik=target_cik,
        current=current_period,
        prior=prior_period,
        pairing_mode=pairing_mode,
        days_between_filings=days_between,
        gaps=overall_gaps,
    )
    is_partial = bool(overall_gaps or current_period.gaps or (prior_period and prior_period.gaps))
    status: Literal["ok", "partial"] = "partial" if is_partial else "ok"
    reason = overall_gaps[0] if overall_gaps else ("partial_exhibit_gaps" if is_partial else "ok")
    cov = Coverage(
        entity_id=eid,
        requested={"cik": target_cik, "pairing_mode": pairing_mode},
        actual={
            "first": prior_period.filed_at.isoformat() if prior_period else current_period.filed_at.isoformat(),
            "last": current_period.filed_at.isoformat(),
        },
        rows=2 if prior_period else 1,
        data_as_of=current_period.filed_at,
        notes=[*overall_gaps, *current_period.gaps, *(prior_period.gaps if prior_period else [])],
    )
    return ToolResult(
        status=status,
        reason=reason,
        coverage=cov,
        permissions=gate,
        provenance=_combine_provenance(adapter, provs, current_period.index_json_url),
        cost=Cost(provider_calls=total_calls),
        data=[payload],
    )


# --------------------------------------------------------------------------- 2. sec_filing_tables (FilingSummary.xml + R*.htm)
def _normalize_menu_category(raw: str) -> Literal["Statements", "Notes", "Tables", "Cover", "Details", "Other"]:
    low = (raw or "").strip().lower()
    if low == "statements":
        return "Statements"
    if low == "notes":
        return "Notes"
    if low == "tables":
        return "Tables"
    if low == "cover":
        return "Cover"
    if low == "details":
        return "Details"
    return "Other"


def parse_filing_summary_xml(
    xml_text: str,
    *,
    cik: str,
    accession: str,
    filing_summary_url: str,
) -> FilingTableCatalog:
    """Deterministically parse SEC ``FilingSummary.xml`` into grouped report entries."""
    target_cik = validate_single_cik(cik)
    dashed, nodash = normalize_accession(accession)
    if _DTD_OR_ENTITY_RE.search(xml_text or ""):
        raise ValueError("XML DTD/ENTITY declarations are not permitted in FilingSummary.xml.")
    root = ET.fromstring(xml_text)

    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{nodash}"
    all_reports: list[FilingReportEntry] = []
    statements: list[FilingReportEntry] = []
    notes: list[FilingReportEntry] = []
    tables: list[FilingReportEntry] = []
    other: list[FilingReportEntry] = []

    for report_el in root.findall(".//MyReports/Report"):
        html_file = (report_el.findtext("HtmlFileName") or "").strip()
        if not html_file or not _R_FILE_RE.fullmatch(html_file):
            continue
        short_name = _clean_text(report_el.findtext("ShortName") or "") or html_file
        long_name = _clean_text(report_el.findtext("LongName") or "") or short_name
        menu_cat = _clean_text(report_el.findtext("MenuCategory") or "") or "Other"
        norm_cat = _normalize_menu_category(menu_cat)
        pos_raw = (report_el.findtext("Position") or "").strip()
        position = int(pos_raw) if pos_raw.isdigit() else None
        role = _clean_text(report_el.findtext("Role") or "") or None
        parent_role = _clean_text(report_el.findtext("ParentRole") or "") or None
        report_type = _clean_text(report_el.findtext("ReportType") or "") or None

        entry = FilingReportEntry(
            r_file=html_file,
            short_name=short_name,
            long_name=long_name,
            menu_category=menu_cat,
            normalized_category=norm_cat,
            position=position,
            role=role,
            parent_role=parent_role,
            report_type=report_type,
            url=f"{base_url}/{html_file}",
        )
        all_reports.append(entry)
        if norm_cat == "Statements":
            statements.append(entry)
        elif norm_cat == "Notes":
            notes.append(entry)
        elif norm_cat == "Tables":
            tables.append(entry)
        else:
            other.append(entry)

    return FilingTableCatalog(
        entity_id=cik_entity_id(target_cik),
        cik=target_cik,
        accession=dashed,
        filing_summary_url=filing_summary_url,
        report_count=len(all_reports),
        statements=statements,
        notes=notes,
        tables=tables,
        other=other,
        all_reports=all_reports,
    )


def sec_filing_tables(
    adapter: SecEdgarAdapter,
    cik: str | int,
    accession: str,
    *,
    category: Literal["Statements", "Notes", "Tables", "Cover", "Details", "Other"] | None = None,
) -> ToolResult:
    """Fetch ``FilingSummary.xml`` for a 10-Q/10-K filing and return grouped report catalog."""
    try:
        target_cik = validate_single_cik(cik)
        dashed, nodash = normalize_accession(accession)
    except ValueError as err:
        return ToolResult.failure("failed", "invalid_request", str(err))

    url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{nodash}/FilingSummary.xml"
    try:
        raw_bytes, ct, prov, cost, gate = _fetch_bytes(
            adapter,
            url,
            "filings",
            accept="application/xml, text/xml",
            not_found_reason="filing_summary_not_found",
            max_bytes=MAX_XML_BYTES,
        )
    except StopWith as stop:
        return stop.result

    xml_text = _decode_bytes(raw_bytes, ct)
    try:
        catalog = parse_filing_summary_xml(xml_text, cik=target_cik, accession=dashed, filing_summary_url=url)
    except (ET.ParseError, ValueError):
        return ToolResult.failure("failed", "invalid_response", "Invalid FilingSummary.xml.", provenance=prov, cost=cost)

    if category is not None:
        filtered = [r for r in catalog.all_reports if r.normalized_category == category]
        catalog = FilingTableCatalog(
            entity_id=catalog.entity_id,
            cik=catalog.cik,
            accession=catalog.accession,
            filing_summary_url=catalog.filing_summary_url,
            report_count=len(filtered),
            statements=[r for r in filtered if r.normalized_category == "Statements"],
            notes=[r for r in filtered if r.normalized_category == "Notes"],
            tables=[r for r in filtered if r.normalized_category == "Tables"],
            other=[r for r in filtered if r.normalized_category not in ("Statements", "Notes", "Tables")],
            all_reports=filtered,
        )

    cov = Coverage(
        entity_id=catalog.entity_id,
        requested={"cik": target_cik, "accession": dashed, "category": category},
        rows=catalog.report_count,
        data_as_of=prov.retrieved_at.date(),
    )
    if catalog.report_count == 0:
        return ToolResult(status="empty", reason="no_reports", coverage=cov, permissions=gate, provenance=prov, cost=cost)
    return ToolResult(status="ok", reason="ok", coverage=cov, permissions=gate, provenance=prov, cost=cost, data=[catalog])


def _extract_currency_and_scale(text: str) -> tuple[str | None, str | None, int | None]:
    currency: str | None = None
    for iso in _ISO_CURRENCIES:
        if re.search(rf"\b{iso}\b", text):
            currency = iso
            break
    if currency is None:
        if "€" in text:
            currency = "EUR"
        elif "£" in text:
            currency = "GBP"
        elif "$" in text:
            currency = "USD"

    low = text.lower()
    scale: str | None = None
    mult: int | None = None
    # Prefer money-specific scale if both "shares in Thousands, $ in Millions" appear
    money_scale = re.search(r"(?:\$|usd|dollars?)\s+in\s+(billions|millions|thousands)", low)
    if money_scale:
        scale = money_scale.group(1)
    elif re.search(r"\bin\s+(billions|millions|thousands)\b", low):
        scale = re.search(r"\bin\s+(billions|millions|thousands)\b", low).group(1)  # type: ignore[union-attr]

    if scale == "billions":
        mult = 1_000_000_000
    elif scale == "millions":
        mult = 1_000_000
    elif scale == "thousands":
        mult = 1_000
    elif "in dollars" in low or "in units" in low:
        scale, mult = "units", 1
    return currency, scale, mult


def _parse_period_meta(header_text: str) -> tuple[str | None, date | None, str | None]:
    cleaned = _clean_text(header_text)
    if not cleaned:
        return None, None, None
    kind: str | None = None
    m_dur = re.search(r"(\d+)\s+Months?\s+Ended", cleaned, re.IGNORECASE)
    if m_dur:
        kind = f"{m_dur.group(1)}M"
    elif re.search(r"\b(?:12\s+Months|Year|Fiscal\s+Year)\s+Ended\b", cleaned, re.IGNORECASE):
        kind = "12M"

    parsed_date: date | None = None
    m_date = re.search(rf"({_MONTH_PATTERN})\.?\s+(\d{{1,2}}),\s+(\d{{4}})", cleaned, re.IGNORECASE)
    if m_date:
        mon = _MONTH_MAP.get(m_date.group(1).lower().rstrip("."))
        if mon:
            try:
                parsed_date = date(int(m_date.group(3)), mon, int(m_date.group(2)))
            except ValueError:
                parsed_date = None
    else:
        m_iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", cleaned)
        if m_iso:
            try:
                parsed_date = date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
            except ValueError:
                parsed_date = None

    if parsed_date is not None and kind is None:
        kind = "instant"
    return cleaned, parsed_date, kind


def _parse_numeric_cell(raw_text: str) -> tuple[Decimal | None, bool, str | None]:
    text = _clean_text(raw_text)
    if not text or text in ("—", "–", "-", "--", "N/A", "n/a", "nil"):
        return None, False, None

    cell_currency: str | None = None
    for sym, iso in _CURRENCY_SYMBOLS.items():
        if sym in text:
            cell_currency = iso
            break
    if cell_currency is None:
        for iso in _ISO_CURRENCIES:
            if re.search(rf"\b{iso}\b", text):
                cell_currency = iso
                break

    # Strip trailing footnote annotations like "1,234 (1)" or "$ 69,958 [2]" (never strip standalone numbers like "$ 12")
    work = re.sub(r"(?<=[\d\)])\s+(?:\(\d{1,2}\)|\[\d{1,2}\]|[†‡*])+$", "", text).strip()
    is_percent = "%" in work
    work = work.replace("%", "").replace("US$", "").replace("$", "").replace("€", "").replace("£", "").replace("¥", "").strip()
    for iso in _ISO_CURRENCIES:
        work = re.sub(rf"\b{iso}\b", "", work).strip()

    negative = False
    if work.startswith("(") and work.endswith(")"):
        negative = True
        work = work[1:-1].strip()
    elif work.startswith("-"):
        negative = True
        work = work[1:].strip()

    work = work.replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", work):
        return None, is_percent, cell_currency
    try:
        val = Decimal(work)
    except InvalidOperation:
        return None, is_percent, cell_currency
    if negative:
        val = -val
    return val, is_percent, cell_currency


def _row_unit_override(label: str, base_currency: str | None, base_scale: str | None, header_note: str) -> tuple[str | None, str | None, int | None]:
    low = label.lower()
    if "per share" in low or "dollars per share" in low:
        return base_currency or "USD", "units", 1
    if "(in shares" in low or "weighted-average shares" in low or "shares outstanding" in low:
        m_shares = re.search(r"shares\s+in\s+(billions|millions|thousands)", header_note.lower())
        if m_shares:
            sc = m_shares.group(1)
            mult = {"billions": 1_000_000_000, "millions": 1_000_000, "thousands": 1_000}[sc]
            return "shares", sc, mult
        return "shares", "units", 1
    return base_currency, base_scale, None


def _build_filing_table_data(
    grid: list[list[_GridCell]],
    *,
    r_file: str,
    url: str,
    table_index: int,
    content_sha256: str,
    provenance: Provenance | None,
    entity_id: str | None,
    accession: str | None,
    narrative_text: str = "",
    fallback_title: str | None = None,
    inherited_note: str = "",
) -> FilingTableData:
    header_rows = 0
    while header_rows < len(grid) and any(c.is_header for c in grid[header_rows]):
        header_rows += 1
    if header_rows == 0 and len(grid) > 1:
        header_rows = 1

    tl_note = grid[0][0].text if grid and grid[0] else ""
    title_line = tl_note.split(" - ")[0].strip() if " - " in tl_note else (tl_note or fallback_title or r_file)
    base_currency, base_scale, base_mult = _extract_currency_and_scale(tl_note)
    if base_currency is None or base_scale is None:
        # Check entire header block and inherited Note header for currency/scale notes
        full_hdr = " ".join(c.text for r in grid[:max(1, header_rows)] for c in r)
        for src_note in (full_hdr, inherited_note):
            if not src_note:
                continue
            hc, hs, hm = _extract_currency_and_scale(src_note)
            base_currency = base_currency or hc
            base_scale = base_scale or hs
            base_mult = base_mult or hm

    width = len(grid[0])
    columns: list[TableColumnMeta] = []
    periods: list[str] = []

    for c in range(1, width):
        parts: list[str] = []
        for r in range(header_rows):
            val = grid[r][c].text.strip()
            if val and (not parts or parts[-1] != val):
                parts.append(val)
        hdr_text = " | ".join(parts) if parts else f"Column {c}"
        p_label, p_end, p_kind = _parse_period_meta(" ".join(parts))
        col_curr, col_scale, col_mult = _extract_currency_and_scale(" ".join(parts))
        if p_label and p_label not in periods:
            periods.append(p_label)
        columns.append(
            TableColumnMeta(
                index=c - 1,
                header=hdr_text,
                period_label=p_label,
                period_end=p_end,
                period_kind=p_kind,
                unit="percent" if "%" in hdr_text else (col_curr or base_currency),
                scale=col_scale or base_scale,
                scale_multiplier=col_mult or base_mult,
                currency=col_curr or base_currency,
            )
        )

    parsed_rows: list[FilingTableRow] = []
    for r_idx, row in enumerate(grid[header_rows:]):
        stub = row[0]
        label = stub.text.strip()
        row_cls = stub.row_class.lower().split()
        is_abstract = "rh" in row_cls or (label.endswith(":") and all(not c.text.strip() for c in row[1:]))
        is_total = any(cls in row_cls for cls in ("reu", "rou", "rt")) or label.lower().startswith("total ")
        row_curr, row_scale, _ = _row_unit_override(label, base_currency, base_scale, tl_note)
        has_row_override = row_curr != base_currency or row_scale != base_scale

        cells: list[FilingTableCell] = []
        values_by_col: dict[str, str] = {}
        for col_idx, cell in enumerate(row[1:]):
            col_meta = columns[col_idx] if col_idx < len(columns) else TableColumnMeta(index=col_idx, header=f"Column {col_idx + 1}")
            raw_val = cell.text.strip()
            num_val, is_pct, c_curr = _parse_numeric_cell(raw_val)
            if base_currency is None and c_curr is not None:
                base_currency = c_curr
            if is_pct:
                effective_curr, effective_scale = None, "percent"
            elif has_row_override:
                effective_curr = row_curr if row_curr == "shares" else (c_curr or row_curr)
                effective_scale = row_scale
            else:
                effective_curr = c_curr or col_meta.currency or row_curr
                effective_scale = col_meta.scale or row_scale
            cells.append(
                FilingTableCell(
                    column_index=col_idx,
                    column_header=col_meta.header,
                    raw_text=raw_val,
                    numeric_value=num_val,
                    is_percent=is_pct,
                    currency=effective_curr,
                    scale=effective_scale,
                    period=col_meta.period_label,
                )
            )
            values_by_col[col_meta.header] = format(num_val, "f") if num_val is not None else raw_val

        if not label and all(not c.raw_text for c in cells):
            continue
        parsed_rows.append(
            FilingTableRow(
                row_index=r_idx,
                label=label,
                indent_level=stub.indent_level,
                is_header=all(c.is_header for c in row),
                is_abstract=is_abstract,
                is_total=is_total,
                xbrl_tag=stub.xbrl_tag,
                cells=cells,
                values_by_column=values_by_col,
            )
        )

    return FilingTableData(
        r_file=r_file,
        title=title_line or r_file,
        url=url,
        table_index=table_index,
        entity_id=entity_id,
        accession=accession,
        currency=base_currency,
        unit_scale=base_scale,
        scale_multiplier=base_mult,
        unit_header_note=tl_note or None,
        periods=periods,
        columns=columns,
        rows=parsed_rows,
        narrative_text=narrative_text,
        subtables=[],
        content_sha256=content_sha256,
        provenance=provenance,
    )


def parse_r_html_table(
    html: str,
    *,
    r_file: str,
    url: str,
    content_sha256: str | None = None,
    provenance: Provenance | None = None,
    entity_id: str | None = None,
    accession: str | None = None,
) -> FilingTableData:
    """Deterministically parse a SEC ``R*.htm`` report into structured rows/columns & metadata."""
    digest = content_sha256 or sha256((html or "").encode("utf-8")).hexdigest()
    parser = _StructuredHtmlParser()
    parser.feed(html or "")
    parser.close()

    raw_tables = [payload for kind, payload in parser._blocks if kind == "table"]
    if not raw_tables:
        return FilingTableData(
            r_file=r_file,
            title=r_file,
            url=url,
            entity_id=entity_id,
            accession=accession,
            narrative_text="\n".join(p for k, p in parser._blocks if k == "text"),
            content_sha256=digest,
            provenance=provenance,
        )

    # Check if primary <table class="report"> is a Note block holding narrative + embedded data subtables
    primary_raw = next((t for t in raw_tables if "report" in t.table_class.lower().split()), raw_tables[0])
    embedded_subtables: list[_RawTable] = []
    narrative_parts: list[str] = []
    for row in primary_raw.rows:
        for cell in row.cells:
            if cell.subtables:
                embedded_subtables.extend(cell.subtables)
                txt = _clean_text(" ".join(cell.text_parts))
                if txt:
                    narrative_parts.append(txt)

    primary_grid = _merge_currency_and_prune_spacers(_expand_table_grid(primary_raw))
    narrative_text = "\n".join(narrative_parts).strip()

    # When primary table is a Note wrapper (<= 2 rows, single data column holding full footnote prose + subtables)
    is_note_wrapper = bool(embedded_subtables) and (not primary_grid or len(primary_grid[0]) <= 2)
    if is_note_wrapper:
        outer_title = primary_grid[0][0].text if primary_grid and primary_grid[0] else r_file
        parsed_subs = [
            _build_filing_table_data(
                _merge_currency_and_prune_spacers(_expand_table_grid(sub)),
                r_file=r_file,
                url=url,
                table_index=idx,
                content_sha256=digest,
                provenance=provenance,
                entity_id=entity_id,
                accession=accession,
                narrative_text=narrative_text if idx == 0 else "",
                fallback_title=outer_title,
                inherited_note=outer_title,
            )
            for idx, sub in enumerate(embedded_subtables)
            if _merge_currency_and_prune_spacers(_expand_table_grid(sub))
        ]
        if parsed_subs:
            lead = parsed_subs[0]
            clean_outer_title = outer_title.split(" (")[0].split(" - ")[0].strip() or outer_title
            return lead.model_copy(
                update={
                    "title": clean_outer_title if len(parsed_subs) == 1 or lead.title == r_file else lead.title,
                    "unit_header_note": lead.unit_header_note or outer_title,
                    "narrative_text": narrative_text,
                    "subtables": parsed_subs,
                }
            )

    if not primary_grid:
        return FilingTableData(
            r_file=r_file,
            title=r_file,
            url=url,
            entity_id=entity_id,
            accession=accession,
            narrative_text=narrative_text,
            content_sha256=digest,
            provenance=provenance,
        )

    main_table = _build_filing_table_data(
        primary_grid,
        r_file=r_file,
        url=url,
        table_index=0,
        content_sha256=digest,
        provenance=provenance,
        entity_id=entity_id,
        accession=accession,
        narrative_text=narrative_text,
    )
    extra_subs: list[FilingTableData] = []
    for idx, sub in enumerate(embedded_subtables, start=1):
        sub_grid = _merge_currency_and_prune_spacers(_expand_table_grid(sub))
        if sub_grid:
            extra_subs.append(
                _build_filing_table_data(
                    sub_grid,
                    r_file=r_file,
                    url=url,
                    table_index=idx,
                    content_sha256=digest,
                    provenance=provenance,
                    entity_id=entity_id,
                    accession=accession,
                    fallback_title=main_table.title,
                )
            )
    if extra_subs:
        main_table = main_table.model_copy(update={"subtables": extra_subs})
    return main_table


def fetch_filing_table(
    adapter: SecEdgarAdapter,
    cik: str | int,
    accession: str,
    r_file: str,
) -> ToolResult:
    """Fetch ``R*.htm`` on demand and deterministically parse structured table data & metadata."""
    try:
        target_cik = validate_single_cik(cik)
        dashed, nodash = normalize_accession(accession)
        clean_r = validate_r_file(r_file)
    except ValueError as err:
        return ToolResult.failure("failed", "invalid_request", str(err))

    url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{nodash}/{clean_r}"
    try:
        raw_bytes, ct, prov, cost, gate = _fetch_bytes(
            adapter,
            url,
            "filings",
            accept="text/html",
            not_found_reason="filing_table_not_found",
            max_bytes=MAX_EXHIBIT_BYTES,
        )
    except StopWith as stop:
        return stop.result

    html_text = _decode_bytes(raw_bytes, ct)
    table = parse_r_html_table(
        html_text,
        r_file=clean_r,
        url=url,
        content_sha256=prov.response_sha256,
        provenance=prov,
        entity_id=cik_entity_id(target_cik),
        accession=dashed,
    )
    cov = Coverage(
        entity_id=cik_entity_id(target_cik),
        requested={"cik": target_cik, "accession": dashed, "r_file": clean_r},
        rows=len(table.rows),
        currency=table.currency,
        data_as_of=prov.retrieved_at.date(),
    )
    if not table.rows and not table.narrative_text and not table.subtables:
        return ToolResult(status="empty", reason="empty_table", coverage=cov, permissions=gate, provenance=prov, cost=cost)
    return ToolResult(status="ok", reason="ok", coverage=cov, permissions=gate, provenance=prov, cost=cost, data=[table])


# --------------------------------------------------------------------------- 3. sec_filing_sections & sec_redline_diff
_PART_LINE_RE = re.compile(r"^\s*PART\s+(I{1,3}|IV)\b[:.\s\-–—]*(.*)$", re.IGNORECASE)
_ITEM_LINE_RE = re.compile(
    r"^\s*(?:PART\s+(I{1,3}|IV)\s*[,.\-–—\s]+)?ITEM\s+(\d+[A-Z]?)\b\s*[.:\-–—]?\s*(.*)$",
    re.IGNORECASE,
)
_TOC_TRAILING_PAGE_RE = re.compile(r"(?:\.{2,}\s*\d{1,4}|\s+\d{1,4}\s*\|?\s*)$")
_MDA_TITLE_RE = re.compile(r"management'?s\s+discussion\s+and\s+analysis", re.IGNORECASE)
_RISK_TITLE_RE = re.compile(r"risk\s+factors", re.IGNORECASE)
_LIQUIDITY_HEADING_RE = re.compile(r"^\s*(?:[A-Z0-9]+\.\s+)?liquidity\s+(?:and|&)\s+capital\s+resources\b", re.IGNORECASE)
_CRITICAL_ACCT_HEADING_RE = re.compile(
    r"^\s*(?:[A-Z0-9]+\.\s+)?critical\s+accounting\s+(?:policies\s+and\s+estimates|estimates|policies)\b",
    re.IGNORECASE,
)
_MDA_PEER_SUBHEADING_RE = re.compile(
    r"^\s*(?:[A-Z0-9]+\.\s+)?(?:"
    r"overview|executive\s+summary|results\s+of\s+operations|segment\s+(?:results|operations)|"
    r"liquidity\s+(?:and|&)\s+capital\s+resources|"
    r"critical\s+accounting\s+(?:policies\s+and\s+estimates|estimates|policies)|"
    r"contractual\s+obligations|off[-\s]?balance\s+sheet|recent\s+accounting\s+pronouncements|"
    r"new\s+accounting\s+(?:standards|pronouncements)|non[-\s]?gaap\s+(?:financial\s+)?measures|"
    r"quantitative\s+and\s+qualitative\s+disclosures\s+about\s+market\s+risk"
    r")\b",
    re.IGNORECASE,
)


_ABBREV_END_RE = re.compile(r"\b(?:Inc|Corp|Co|Ltd|LLC|U\.S|No|vs|Mr|Ms|Dr|Jan|Feb|Aug|Sep|Sept|Oct|Nov|Dec)\.$", re.IGNORECASE)


def _split_paragraphs(text: str) -> list[str]:
    paras: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            joined = " ".join(buf).strip()
            if joined and not re.fullmatch(r"\d{1,4}", joined) and joined.lower() != "table of contents":
                paras.append(joined)
            buf.clear()

    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        if s.startswith("|") and s.endswith("|"):
            flush()
            paras.append(s)
            continue
        if s.startswith(("•", "-", "*")) and buf:
            flush()
        buf.append(s)
        ends_sentence = s.endswith((".", "!", "?", '."', ".'", ".)")) and not _ABBREV_END_RE.search(s)
        is_heading_line = len(s) < 95 and not s.endswith((",", ";")) and len(buf) == 1
        if ends_sentence or is_heading_line:
            flush()
    flush()
    return paras


def _canonical_item_key(item_num: str, title: str, part: str | None, form: str | None) -> str:
    num = item_num.upper()
    low_title = title.lower()
    is_10k = (form or "").upper().startswith("10-K")
    is_10q = (form or "").upper().startswith("10-Q")

    if num == "1A" or _RISK_TITLE_RE.search(title):
        return "item_1a_risk_factors"
    if num == "7" or _MDA_TITLE_RE.search(title):
        return "mda"
    if num == "2":
        if _MDA_TITLE_RE.search(title):
            return "mda"
        if "properties" in low_title or is_10k:
            return "item_2_properties"
        if "unregistered" in low_title or "issuer purchases" in low_title or part == "Part II":
            return "part2_item2_unregistered_sales"
        if is_10q and part == "Part I":
            return "mda"
        return "item_2_properties"
    if num == "1":
        if "business" in low_title or is_10k:
            return "item_1_business"
        if "financial statements" in low_title or (is_10q and part == "Part I"):
            return "part1_item1_financial_statements"
        if "legal proceedings" in low_title or part == "Part II":
            return "item_3_legal_proceedings"
        return "item_1_business"
    if num == "1C":
        return "item_1c_cybersecurity"
    if num == "3":
        if "quantitative" in low_title or "market risk" in low_title or (is_10q and part == "Part I"):
            return "item_7a_market_risk"
        return "item_3_legal_proceedings"
    if num == "7A":
        return "item_7a_market_risk"
    if num == "8":
        return "item_8_financial_statements"
    return f"item_{num.lower()}"


def _make_section(
    key: str,
    title: str,
    text: str,
    *,
    char_start: int,
    char_end: int,
    item_number: str | None = None,
    part: str | None = None,
) -> FilingSection:
    body = text.strip()
    paras = _split_paragraphs(body)
    return FilingSection(
        section_key=key,
        item_number=item_number,
        part=part,
        title=title,
        text=body,
        paragraphs=paras,
        char_start=char_start,
        char_end=char_end,
        char_count=len(body),
        word_count=len(body.split()),
        content_sha256=sha256(body.encode("utf-8")).hexdigest(),
    )


def _extract_mda_subsections(mda_section: FilingSection) -> list[FilingSection]:
    """Extract ``liquidity_and_capital_resources`` and ``critical_accounting_estimates`` within MD&A."""
    lines = mda_section.text.splitlines()
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    markers: list[tuple[int, str, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 130 or stripped.startswith("|"):
            continue
        if stripped.endswith(".") and len(stripped) > 65:
            continue
        if _LIQUIDITY_HEADING_RE.match(stripped):
            markers.append((idx, "liquidity_and_capital_resources", stripped))
        elif _CRITICAL_ACCT_HEADING_RE.match(stripped):
            markers.append((idx, "critical_accounting_estimates", stripped))
        elif _MDA_PEER_SUBHEADING_RE.match(stripped):
            markers.append((idx, "__peer__", stripped))

    extracted: list[FilingSection] = []
    seen: set[str] = set()
    for m_idx, (line_idx, key, heading_title) in enumerate(markers):
        if key == "__peer__" or key in seen:
            continue
        end_line = markers[m_idx + 1][0] if m_idx + 1 < len(markers) else len(lines)
        sub_text = "\n".join(lines[line_idx:end_line]).strip()
        if len(sub_text) < 40:
            continue
        seen.add(key)
        c_start = mda_section.char_start + offsets[line_idx]
        c_end = mda_section.char_start + (offsets[end_line] if end_line < len(offsets) else len(mda_section.text))
        extracted.append(
            _make_section(
                key,
                heading_title,
                sub_text,
                char_start=c_start,
                char_end=c_end,
                item_number=mda_section.item_number,
                part=mda_section.part,
            )
        )
    return extracted


def split_filing_sections(
    content: str,
    *,
    form: str | None = None,
    is_html: bool | None = None,
    provenance: Provenance | None = None,
) -> FilingSectionsBundle:
    """Deterministically split 10-K / 10-Q HTML or text into canonical Item & MD&A subsections."""
    raw = content or ""
    if is_html is None:
        is_html = bool(re.search(r"<(?:html|body|div|table|p|span|ix:)\b", raw[:4096], re.IGNORECASE))
    clean_text = strip_html_preserving_tables(raw)[0] if is_html else raw
    clean_text = "\n".join(_clean_text(line) if not line.startswith("|") else line.strip() for line in clean_text.splitlines())

    lines = clean_text.splitlines()
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    @dataclass
    class _HeadingHit:
        line_idx: int
        char_offset: int
        key: str
        item_number: str
        part: str | None
        title: str
        looks_like_toc: bool

    hits: list[_HeadingHit] = []
    current_part: str | None = None

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue

        # Track Part I / Part II boundaries
        m_part = _PART_LINE_RE.match(stripped)
        if m_part and len(stripped) <= 90 and not stripped.startswith("|") and not _TOC_TRAILING_PAGE_RE.search(stripped):
            part_roman = m_part.group(1).upper()
            rest = m_part.group(2).strip()
            if not rest or not re.match(r"^(?:of|in|and|to)\b", rest, re.IGNORECASE):
                current_part = f"Part {part_roman}"

        m_item = _ITEM_LINE_RE.match(stripped)
        if m_item and not stripped.startswith("|"):
            inline_part, item_num, title_rest = m_item.groups()
            item_num = item_num.upper()
            part_here = f"Part {inline_part.upper()}" if inline_part else current_part

            # Combine 2-line wrapped heading ("Item 1A.\nRisk Factors")
            title_text = title_rest.strip()
            if not title_text and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and len(nxt) <= 130 and not _ITEM_LINE_RE.match(nxt) and not nxt.startswith("|"):
                    title_text = nxt

            looks_toc = bool(_TOC_TRAILING_PAGE_RE.search(stripped) or (not title_rest.strip() and title_text and _TOC_TRAILING_PAGE_RE.search(title_text)))
            # Reject mid-sentence cross-references (too long or ending with period after long sentence)
            is_too_long = len(stripped) > 160
            # Require either known Item phrasing or reasonably bounded heading length
            if not is_too_long:
                key = _canonical_item_key(item_num, title_text, part_here, form)
                display_title = f"Item {item_num}. {title_text}".strip() if title_text else f"Item {item_num}"
                hits.append(
                    _HeadingHit(
                        line_idx=i,
                        char_offset=offsets[i],
                        key=key,
                        item_number=f"Item {item_num}",
                        part=part_here,
                        title=re.sub(r"\s+", " ", display_title).rstrip("."),
                        looks_like_toc=looks_toc,
                    )
                )
        i += 1

    # Filter out TOC clusters and pick real section spans
    selected: list[_HeadingHit] = []
    for idx, hit in enumerate(hits):
        next_offset = hits[idx + 1].char_offset if idx + 1 < len(hits) else len(clean_text)
        span_len = next_offset - hit.char_offset
        if hit.looks_like_toc:
            continue
        # If the exact same section key appears later with a real body and this span is tiny (< 220 chars), skip this TOC line
        has_later_larger = any(
            other.key == hit.key
            and not other.looks_like_toc
            and (
                (hits[o_idx + 1].char_offset if o_idx + 1 < len(hits) else len(clean_text)) - other.char_offset
            ) > span_len * 2
            for o_idx, other in enumerate(hits[idx + 1:], start=idx + 1)
        )
        if span_len < 220 and has_later_larger:
            continue
        selected.append(hit)

    sections: dict[str, FilingSection] = {}
    section_order: list[str] = []
    for idx, hit in enumerate(selected):
        c_start = hit.char_offset
        c_end = selected[idx + 1].char_offset if idx + 1 < len(selected) else len(clean_text)
        sec_text = clean_text[c_start:c_end].strip()
        sec = _make_section(
            hit.key,
            hit.title,
            sec_text,
            char_start=c_start,
            char_end=c_end,
            item_number=hit.item_number,
            part=hit.part,
        )
        # Keep the longest section if a key appeared twice (e.g., stub vs full body)
        existing = sections.get(hit.key)
        if existing is None or sec.char_count > existing.char_count:
            sections[hit.key] = sec
            if hit.key not in section_order:
                section_order.append(hit.key)

    # Extract MD&A subsections (Liquidity & Capital Resources, Critical Accounting Estimates)
    if "mda" in sections:
        for sub in _extract_mda_subsections(sections["mda"]):
            if sub.section_key not in sections:
                sections[sub.section_key] = sub
                section_order.append(sub.section_key)
    else:
        # Fallback if filing excerpt only contained MD&A subsections without Item 2/7 heading
        pseudo_mda = _make_section("mda", "MD&A", clean_text, char_start=0, char_end=len(clean_text))
        for sub in _extract_mda_subsections(pseudo_mda):
            sections[sub.section_key] = sub
            section_order.append(sub.section_key)

    return FilingSectionsBundle(
        form=form,
        sections=sections,
        section_order=section_order,
        clean_text=clean_text,
        content_sha256=sha256(clean_text.encode("utf-8")).hexdigest(),
        provenance=provenance,
    )


def sec_filing_sections(
    adapter: SecEdgarAdapter | None = None,
    *,
    url: str | None = None,
    html: str | None = None,
    text: str | None = None,
    form: str | None = None,
) -> ToolResult:
    """Split a 10-K / 10-Q filing (from URL via adapter, or directly supplied HTML/text) into Item sections."""
    prov: Provenance | None = None
    cost = Cost()
    perms = Permissions(store=True, display=True, model_input=True, export=True)

    if url is not None:
        if adapter is None:
            return ToolResult.failure("failed", "invalid_request", "adapter is required when fetching a filing URL.")
        try:
            raw_bytes, ct, prov, cost, perms = _fetch_bytes(
                adapter,
                url,
                "filings",
                accept="text/html, text/plain",
                not_found_reason="filing_not_found",
                large=True,
                max_bytes=MAX_FILING_HTML_BYTES,
            )
        except StopWith as stop:
            return stop.result
        bundle = split_filing_sections(_decode_bytes(raw_bytes, ct), form=form, is_html=True, provenance=prov)
    elif html is not None:
        bundle = split_filing_sections(html, form=form, is_html=True)
    elif text is not None:
        bundle = split_filing_sections(text, form=form, is_html=False)
    else:
        return ToolResult.failure("failed", "invalid_request", "Supply url, html, or text.")

    cov = Coverage(
        requested={"form": form, "url": url},
        rows=len(bundle.sections),
        data_as_of=prov.retrieved_at.date() if prov else None,
    )
    if not bundle.sections:
        return ToolResult(status="empty", reason="no_sections_detected", coverage=cov, permissions=perms, provenance=prov, cost=cost)
    return ToolResult(status="ok", reason="ok", coverage=cov, permissions=perms, provenance=prov, cost=cost, data=[bundle])


# ---- Redline Diff with rolling noise filter ----
_DATE_WRITTEN_RE = re.compile(rf"\b{_MONTH_PATTERN}\.?\s+\d{{1,2}}(?:,\s*|\s+)\d{{4}}\b", re.IGNORECASE)
_MONTH_YEAR_RE = re.compile(rf"\b{_MONTH_PATTERN}\.?\s+\d{{4}}\b", re.IGNORECASE)
_DATE_ISO_SLASH_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b")
_FISCAL_YEAR_RE = re.compile(r"\b(?:fiscal\s+(?:year\s+)?\d{4}|FY\s*['’]?\d{2,4})\b", re.IGNORECASE)
_QUARTER_RE = re.compile(r"\b(?:Q[1-4](?:\s*\d{2,4})?|(?:first|second|third|fourth)\s+quarter(?:\s+of\s+\d{4})?)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_NUMBER_RE = re.compile(r"(?:US\$|[$€£¥])?\s*\(?\d[\d,]*(?:\.\d+)?\)?%?", re.IGNORECASE)


def _normalize_para(text: str) -> str:
    return re.sub(r"\s+", " ", _clean_text(text)).strip()


def _rolling_skeleton(text: str) -> str:
    """Normalize dates, fiscal periods, years, and numeric magnitudes to detect pure rolling noise."""
    work = _normalize_para(text).lower()
    work = _DATE_WRITTEN_RE.sub("<DATE>", work)
    work = _DATE_ISO_SLASH_RE.sub("<DATE>", work)
    work = _MONTH_YEAR_RE.sub("<DATE>", work)
    work = _FISCAL_YEAR_RE.sub("<YEAR>", work)
    work = _QUARTER_RE.sub("<QUARTER>", work)
    work = _YEAR_RE.sub("<YEAR>", work)
    work = _NUMBER_RE.sub("<NUM>", work)
    work = re.sub(r"\s+", " ", work).strip()
    return work


def _word_redline_markup(prior_text: str, current_text: str) -> str:
    p_words = prior_text.split()
    c_words = current_text.split()
    sm = SequenceMatcher(None, p_words, c_words, autojunk=False)
    chunks: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            chunks.append(" ".join(c_words[j1:j2]))
        elif tag == "delete":
            chunks.append(f"[-{' '.join(p_words[i1:i2])}-]")
        elif tag == "insert":
            chunks.append(f"[+{' '.join(c_words[j1:j2])}+]")
        elif tag == "replace":
            chunks.append(f"[-{' '.join(p_words[i1:i2])}-] [+{' '.join(c_words[j1:j2])}+]")
    return " ".join(c for c in chunks if c)


def _paragraphs_from_input(obj: FilingSection | str | Sequence[str]) -> list[str]:
    if isinstance(obj, FilingSection):
        return list(obj.paragraphs)
    if isinstance(obj, str):
        return _split_paragraphs(obj)
    return [_normalize_para(str(p)) for p in obj if _normalize_para(str(p))]


def diff_section_paragraphs(
    current: FilingSection | str | Sequence[str],
    prior: FilingSection | str | Sequence[str],
    *,
    section_key: str | None = None,
    filter_rolling_noise: bool = True,
    similarity_threshold: float = 0.45,
) -> SectionRedlineDiff:
    """Compare current vs prior section paragraphs, filtering pure date/number rolling noise."""
    cur_raw = _paragraphs_from_input(current)
    pri_raw = _paragraphs_from_input(prior)

    cur_norm = [_normalize_para(p) for p in cur_raw]
    pri_norm = [_normalize_para(p) for p in pri_raw]
    cur_skel = [_rolling_skeleton(p) for p in cur_raw]
    pri_skel = [_rolling_skeleton(p) for p in pri_raw]

    matched_cur: set[int] = set()
    matched_pri: set[int] = set()
    unchanged_count = 0
    noise_count = 0
    noise_examples: list[str] = []

    # 1. Exact text matches (handles reordered identical paragraphs)
    pri_exact_buckets: dict[str, list[int]] = {}
    for p_idx, txt in enumerate(pri_norm):
        pri_exact_buckets.setdefault(txt, []).append(p_idx)
    for c_idx, txt in enumerate(cur_norm):
        bucket = pri_exact_buckets.get(txt)
        if bucket:
            p_idx = bucket.pop(0)
            matched_cur.add(c_idx)
            matched_pri.add(p_idx)
            unchanged_count += 1

    # 2. Pure date/number rolling noise matches (skeleton identical, raw text differs)
    if filter_rolling_noise:
        pri_skel_buckets: dict[str, list[int]] = {}
        for p_idx, skel in enumerate(pri_skel):
            if p_idx not in matched_pri:
                pri_skel_buckets.setdefault(skel, []).append(p_idx)
        for c_idx, skel in enumerate(cur_skel):
            if c_idx in matched_cur:
                continue
            bucket = pri_skel_buckets.get(skel)
            if bucket:
                p_idx = bucket.pop(0)
                matched_cur.add(c_idx)
                matched_pri.add(p_idx)
                noise_count += 1
                if len(noise_examples) < 5:
                    noise_examples.append(f"{pri_raw[p_idx]} => {cur_raw[c_idx]}")

    rem_cur = [i for i in range(len(cur_raw)) if i not in matched_cur]
    rem_pri = [i for i in range(len(pri_raw)) if i not in matched_pri]

    # 3. Pair remaining paragraphs into revised vs added/removed using SequenceMatcher + best similarity within hunks
    added: list[RedlineParagraphDiff] = []
    removed: list[RedlineParagraphDiff] = []
    revised: list[RedlineParagraphDiff] = []

    sm = SequenceMatcher(None, [pri_skel[i] for i in rem_pri], [cur_skel[i] for i in rem_cur], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # Defensive: shouldn't happen when filter_rolling_noise=True, but when False this preserves equal skeletons as revised
            for p_pos, c_pos in zip(range(i1, i2), range(j1, j2)):
                pi, ci = rem_pri[p_pos], rem_cur[c_pos]
                revised.append(
                    RedlineParagraphDiff(
                        change_type="revised",
                        section_key=section_key,
                        current_index=ci,
                        prior_index=pi,
                        current_text=cur_raw[ci],
                        prior_text=pri_raw[pi],
                        similarity=round(SequenceMatcher(None, pri_norm[pi].split(), cur_norm[ci].split()).ratio(), 3),
                        redline_markup=_word_redline_markup(pri_raw[pi], cur_raw[ci]),
                    )
                )
        elif tag == "delete":
            for p_pos in range(i1, i2):
                pi = rem_pri[p_pos]
                removed.append(
                    RedlineParagraphDiff(
                        change_type="removed",
                        section_key=section_key,
                        prior_index=pi,
                        prior_text=pri_raw[pi],
                    )
                )
        elif tag == "insert":
            for c_pos in range(j1, j2):
                ci = rem_cur[c_pos]
                added.append(
                    RedlineParagraphDiff(
                        change_type="added",
                        section_key=section_key,
                        current_index=ci,
                        current_text=cur_raw[ci],
                    )
                )
        elif tag == "replace":
            p_indices = [rem_pri[k] for k in range(i1, i2)]
            c_indices = [rem_cur[k] for k in range(j1, j2)]
            pairs: list[tuple[float, int, int]] = []
            for pi in p_indices:
                p_words = pri_norm[pi].split()
                for ci in c_indices:
                    c_words = cur_norm[ci].split()
                    sim = SequenceMatcher(None, p_words, c_words, autojunk=False).ratio()
                    if sim >= similarity_threshold:
                        pairs.append((sim, pi, ci))
            pairs.sort(reverse=True)
            used_p: set[int] = set()
            used_c: set[int] = set()
            for sim, pi, ci in pairs:
                if pi in used_p or ci in used_c:
                    continue
                used_p.add(pi)
                used_c.add(ci)
                revised.append(
                    RedlineParagraphDiff(
                        change_type="revised",
                        section_key=section_key,
                        current_index=ci,
                        prior_index=pi,
                        current_text=cur_raw[ci],
                        prior_text=pri_raw[pi],
                        similarity=round(sim, 3),
                        redline_markup=_word_redline_markup(pri_raw[pi], cur_raw[ci]),
                    )
                )
            for pi in p_indices:
                if pi not in used_p:
                    removed.append(
                        RedlineParagraphDiff(
                            change_type="removed",
                            section_key=section_key,
                            prior_index=pi,
                            prior_text=pri_raw[pi],
                        )
                    )
            for ci in c_indices:
                if ci not in used_c:
                    added.append(
                        RedlineParagraphDiff(
                            change_type="added",
                            section_key=section_key,
                            current_index=ci,
                            current_text=cur_raw[ci],
                        )
                    )

    added.sort(key=lambda d: d.current_index or 0)
    removed.sort(key=lambda d: d.prior_index or 0)
    revised.sort(key=lambda d: d.current_index or 0)
    has_changes = bool(added or removed or revised)
    return SectionRedlineDiff(
        section_key=section_key,
        added=added,
        removed=removed,
        revised=revised,
        unchanged_count=unchanged_count,
        filtered_noise_count=noise_count,
        filtered_noise_examples=noise_examples,
        has_substantive_changes=has_changes,
    )


def diff_filing_sections(
    current: FilingSectionsBundle,
    prior: FilingSectionsBundle,
    *,
    section_keys: Iterable[str] | None = None,
    filter_rolling_noise: bool = True,
    similarity_threshold: float = 0.45,
) -> FilingRedlineReport:
    """Compare two ``FilingSectionsBundle`` objects section-by-section."""
    if section_keys is not None:
        keys = list(dict.fromkeys(section_keys))
    else:
        keys = list(dict.fromkeys([*current.section_order, *prior.section_order]))

    diffs: dict[str, SectionRedlineDiff] = {}
    tot_a = tot_r = tot_v = tot_n = 0
    for key in keys:
        cur_sec = current.sections.get(key)
        pri_sec = prior.sections.get(key)
        if cur_sec is None and pri_sec is None:
            continue
        diff = diff_section_paragraphs(
            cur_sec if cur_sec is not None else [],
            pri_sec if pri_sec is not None else [],
            section_key=key,
            filter_rolling_noise=filter_rolling_noise,
            similarity_threshold=similarity_threshold,
        )
        diffs[key] = diff
        tot_a += len(diff.added)
        tot_r += len(diff.removed)
        tot_v += len(diff.revised)
        tot_n += diff.filtered_noise_count
    return FilingRedlineReport(
        section_diffs=diffs,
        total_added=tot_a,
        total_removed=tot_r,
        total_revised=tot_v,
        total_filtered_noise=tot_n,
    )


def sec_redline_diff(
    current: FilingSectionsBundle | FilingSection | str | Sequence[str],
    prior: FilingSectionsBundle | FilingSection | str | Sequence[str],
    *,
    section_keys: Iterable[str] | None = None,
    section_key: str | None = None,
    filter_rolling_noise: bool = True,
    similarity_threshold: float = 0.45,
) -> FilingRedlineReport | SectionRedlineDiff:
    """Run cross-period Redline Diff on either full ``FilingSectionsBundle`` pairs or single sections."""
    if isinstance(current, FilingSectionsBundle) and isinstance(prior, FilingSectionsBundle):
        return diff_filing_sections(
            current,
            prior,
            section_keys=section_keys,
            filter_rolling_noise=filter_rolling_noise,
            similarity_threshold=similarity_threshold,
        )
    if isinstance(current, FilingSectionsBundle) or isinstance(prior, FilingSectionsBundle):
        raise TypeError("Both current and prior must be FilingSectionsBundle when diffing filing bundles.")
    return diff_section_paragraphs(
        current,
        prior,
        section_key=section_key,
        filter_rolling_noise=filter_rolling_noise,
        similarity_threshold=similarity_threshold,
    )


# --------------------------------------------------------------------------- 4. cik_scoped_efts_search
def cik_scoped_efts_search(
    adapter: SecEdgarAdapter,
    cik: str | int | None,
    query: str,
    *,
    start: date | None = None,
    end: date | None = None,
    forms: Iterable[str] | None = None,
    limit: int = 50,
) -> ToolResult:
    """Strictly single-CIK-scoped SEC EFTS full-text search (never permits unscoped global search)."""
    try:
        target_cik = validate_single_cik(cik)
    except ValueError as err:
        return ToolResult.failure("failed", "invalid_request", str(err))

    q = (query or "").strip()
    if not q:
        return ToolResult.failure("failed", "invalid_request", "query must not be blank.")
    if limit < 1 or limit > 100:
        return ToolResult.failure("failed", "invalid_request", "limit must be between 1 and 100.")
    if start is not None and end is not None and start > end:
        return ToolResult.failure("failed", "invalid_request", "start date must not be after end date.")

    params: dict[str, str] = {"q": q, "ciks": target_cik}
    if start or end:
        params.update({
            "dateRange": "custom",
            "startdt": (start or date(2001, 1, 1)).isoformat(),
            "enddt": (end or adapter._now().date()).isoformat(),
        })
    cleaned_forms = [f.strip().upper() for f in (forms or ()) if str(f).strip()]
    if cleaned_forms:
        params["forms"] = ",".join(cleaned_forms)

    url = f"{EFTS_URL}?{urlencode(params, quote_via=quote)}"
    search_category = "filing_search_cik"
    if isinstance(adapter._policy.decide(PROVIDER, search_category), Denied) and not isinstance(
        adapter._policy.decide(PROVIDER, "filing_search"), Denied
    ):
        search_category = "filing_search"
    try:
        doc, prov, cost, perms = adapter._get(url, search_category)
    except StopWith as stop:
        return stop.result

    hits = ((doc.get("hits") or {}).get("hits")) if isinstance(doc, Mapping) else None
    if not isinstance(hits, list):
        return ToolResult.failure("failed", "invalid_response", "Unexpected EFTS full-text search shape.", provenance=prov, cost=cost)

    total = (doc["hits"].get("total") or {}).get("value")
    rows: list[FullTextHit] = []
    dropped_out_of_scope = 0

    for h in hits:
        if len(rows) >= limit:
            break
        src = h.get("_source") if isinstance(h, Mapping) else None
        if not isinstance(src, Mapping):
            continue
        raw_ciks = src.get("ciks") or []
        norm_ciks: list[str] = []
        for raw_c in raw_ciks:
            try:
                norm_ciks.append(cik10(raw_c))
            except ValueError:
                continue
        if target_cik not in norm_ciks:
            dropped_out_of_scope += 1
            continue
        acc = src.get("adsh")
        doc_id = str(h.get("_id", "")).partition(":")[2] or None
        hit_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{acc.replace('-', '')}/{doc_id}"
            if isinstance(acc, str) and doc_id
            else None
        )
        names = src.get("display_names") or []
        rows.append(
            FullTextHit(
                entity_id=cik_entity_id(target_cik),
                ciks=norm_ciks,
                display_name=names[0] if names else None,
                form=src.get("form"),
                filed_at=_parse_date(src.get("file_date")),
                period_of_report=_parse_date(src.get("period_ending")),
                accession=acc if isinstance(acc, str) else None,
                document=doc_id,
                document_type=src.get("file_type"),
                description=src.get("file_description"),
                url=hit_url,
            )
        )

    notes = [
        f"Scoped strictly to single CIK {target_cik}.",
        "EDGAR full-text search is an undocumented-but-public endpoint "
        "(https://efts.sec.gov/LATEST/search-index); coverage and shape may change.",
        f"Provider reported {total} total hits.",
    ]
    if dropped_out_of_scope:
        notes.append(f"Dropped {dropped_out_of_scope} hit(s) outside requested CIK {target_cik}.")

    truncated = isinstance(total, int) and total > len(rows)
    cov = Coverage(
        entity_id=cik_entity_id(target_cik),
        requested={
            "cik": target_cik,
            "query": q,
            "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None,
            "forms": ",".join(cleaned_forms) if cleaned_forms else None,
        },
        rows=len(rows),
        data_as_of=prov.retrieved_at.date(),
        truncated=truncated,
        notes=notes,
    )
    if not rows:
        return ToolResult(status="empty", reason="no_hits", coverage=cov, permissions=perms, provenance=prov, cost=cost)
    return ToolResult(
        status="partial" if truncated else "ok",
        reason="truncated" if truncated else "ok",
        coverage=cov,
        permissions=perms,
        provenance=prov,
        cost=cost,
        data=rows,
    )


# --------------------------------------------------------------------------- Ergonomic wrapper class
class SecDeepAdapter:
    """Facade wrapping ``SecEdgarAdapter`` with the 4 Phase A1 SEC deep parsing methods."""

    def __init__(
        self,
        user_agent: str | None = None,
        *,
        adapter: SecEdgarAdapter | None = None,
        transport: Transport | None = None,
        limiter=None,
        policy: FinDataPolicy | None = None,
        now_fn: Callable[[], datetime] = utcnow,
        timeout: float = 15.0,
        stream_transport: StreamTransport | None = None,
    ) -> None:
        self._adapter = adapter or SecEdgarAdapter(
            user_agent,
            transport=transport,
            limiter=limiter,
            policy=policy,
            now_fn=now_fn,
            timeout=timeout,
            stream_transport=stream_transport,
        )

    @property
    def sec_adapter(self) -> SecEdgarAdapter:
        return self._adapter

    def earnings_release(
        self,
        cik: str | int,
        *,
        submissions: Sequence[Filing] | ToolResult | Mapping[str, Any] | None = None,
        as_of: date | None = None,
        pairing_mode: Literal["sequential", "yoy"] = "sequential",
        include_ex99_2: bool = True,
        max_candidates: int = 15,
    ) -> ToolResult:
        return sec_earnings_release(
            self._adapter,
            cik,
            submissions=submissions,
            as_of=as_of,
            pairing_mode=pairing_mode,
            include_ex99_2=include_ex99_2,
            max_candidates=max_candidates,
        )

    def filing_tables(
        self,
        cik: str | int,
        accession: str,
        *,
        category: Literal["Statements", "Notes", "Tables", "Cover", "Details", "Other"] | None = None,
    ) -> ToolResult:
        return sec_filing_tables(self._adapter, cik, accession, category=category)

    def fetch_filing_table(self, cik: str | int, accession: str, r_file: str) -> ToolResult:
        return fetch_filing_table(self._adapter, cik, accession, r_file)

    def filing_sections(
        self,
        *,
        url: str | None = None,
        html: str | None = None,
        text: str | None = None,
        form: str | None = None,
    ) -> ToolResult:
        return sec_filing_sections(self._adapter, url=url, html=html, text=text, form=form)

    def redline_diff(
        self,
        current: FilingSectionsBundle | FilingSection | str | Sequence[str],
        prior: FilingSectionsBundle | FilingSection | str | Sequence[str],
        *,
        section_keys: Iterable[str] | None = None,
        section_key: str | None = None,
        filter_rolling_noise: bool = True,
        similarity_threshold: float = 0.45,
    ) -> FilingRedlineReport | SectionRedlineDiff:
        return sec_redline_diff(
            current,
            prior,
            section_keys=section_keys,
            section_key=section_key,
            filter_rolling_noise=filter_rolling_noise,
            similarity_threshold=similarity_threshold,
        )

    def cik_scoped_efts_search(
        self,
        cik: str | int | None,
        query: str,
        *,
        start: date | None = None,
        end: date | None = None,
        forms: Iterable[str] | None = None,
        limit: int = 50,
    ) -> ToolResult:
        return cik_scoped_efts_search(self._adapter, cik, query, start=start, end=end, forms=forms, limit=limit)


__all__ = [
    "DualPeriodEarningsRelease",
    "EarningsReleaseExhibit",
    "EarningsReleasePeriod",
    "FilingRedlineReport",
    "FilingReportEntry",
    "FilingSection",
    "FilingSectionsBundle",
    "FilingTableCatalog",
    "FilingTableCell",
    "FilingTableData",
    "FilingTableRow",
    "GuidancePassage",
    "NonGaapReconciliation",
    "ParsedHtmlTable",
    "RedlineParagraphDiff",
    "SecDeepAdapter",
    "SectionRedlineDiff",
    "TableColumnMeta",
    "cik_scoped_efts_search",
    "diff_filing_sections",
    "diff_section_paragraphs",
    "fetch_filing_table",
    "normalize_accession",
    "parse_filing_summary_xml",
    "parse_r_html_table",
    "sec_earnings_release",
    "sec_filing_sections",
    "sec_filing_tables",
    "sec_redline_diff",
    "split_filing_sections",
    "strip_html_preserving_tables",
    "validate_r_file",
    "validate_single_cik",
]
