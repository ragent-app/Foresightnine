"""Bounded retrieval of public HTTPS documents (HTML, plain text, PDF)."""
from __future__ import annotations

import base64
import codecs
import io
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import html as html_module
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
    OCR_CANDIDATE_STATUSES,
    TEXT_STATUSES,
    TextPager,
    PdfPagesError,
    merge_ocr_into_document,
    extract_pdf_pages,
    pdf_extract_budget_seconds,
    pdf_page_count,
    page_map_from_text,
    prioritize_ocr_pages,
)
from .phase3 import ClientSearchEvidenceProvider, ProviderFailure
from .research_source_policy import is_fred_source
from .research_source_terms import terms_decision
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
_PRESERVE_ROLES = frozenset({"main", "article"})
_DENSITY_CANDIDATE_TAGS = frozenset({"div", "section"})
_MAX_DENSITY_BUFFER_CHARS = 16_384
_MAX_DENSITY_BUFFER_PARTS = 8_192

_EXACT_BOILERPLATE_ATTRS = frozenset({
    "ad", "ads", "advert", "adverts", "advertisement", "advertisements", "adsense", "adserver",
    "adslot", "adunit", "adbox", "adbanner", "adwrapper", "adcontainer", "taboola", "outbrain",
    "cookie", "cookies", "cookiebanner", "cookieconsent", "cookiebar", "cookienotice", "cookielaw",
    "cookiepolicy", "cookiebot", "onetrust", "didomi", "complianz", "cookieyes", "termly",
    "quantcast", "trustarc", "consentmanager",
    "comments", "comment-section", "comments-section", "comment-list", "comments-list",
    "comment-box", "comments-box", "comment-form", "comments-form", "comment-thread",
    "comments-thread", "comment-area", "comments-area", "comment-wrapper", "comments-wrapper",
    "comment-container", "comments-container", "comment-respond", "disqus_thread",
    "newsletter", "newsletters", "newsletter-signup", "newsletter-box", "subscribe-box",
    "subscribe-banner", "subscribe-widget", "email-signup", "signup-box",
    "breadcrumb", "breadcrumbs", "share-buttons", "share-bar", "share-links", "share-widget",
    "social-share", "social-links", "social-buttons", "around-the-web", "you-might-like",
    "you-may-like", "read-next", "also-read", "recirculation",
})
_BOILERPLATE_SUBTOKENS = frozenset({
    "ads", "advert", "adverts", "advertisement", "advertisements", "adsense", "adserver",
    "adslot", "adunit", "adbox", "adbanner", "adwrapper", "adcontainer", "doubleclick",
    "googlesyndication", "taboola", "outbrain", "dfp",
    "cookiebanner", "cookieconsent", "cookiebar", "cookienotice", "cookielaw", "cookiepolicy",
    "cookiebot", "onetrust", "didomi", "complianz", "cookieyes", "termly", "quantcast",
    "trustarc", "consentmanager",
    "disqus", "utterances", "giscus", "livefyre", "spotim", "commentbox", "commentsbox",
    "commentsection", "commentssection", "breadcrumbs", "recirculation",
})
_AD_QUALIFIERS = frozenset({
    "banner", "slot", "unit", "box", "block", "container", "wrapper", "section", "area",
    "module", "widget", "sidebar", "header", "footer", "bar", "leaderboard", "skyscraper",
    "overlay", "holder", "frame", "label", "tag", "sense", "choice", "choices",
    "top", "bottom", "left", "right", "mid", "inline", "native", "promo", "sponsor", "sponsored",
})
_SPONSORED_QUALIFIERS = frozenset({
    "content", "link", "links", "post", "posts", "article", "articles", "ad", "ads",
    "box", "card", "widget", "section", "block", "banner", "slot", "listing",
})
_COOKIE_QUALIFIERS = frozenset({
    "banner", "consent", "notice", "policy", "bar", "popup", "modal", "wall", "overlay",
    "settings", "preferences", "dialog", "message", "alert", "wrapper", "container",
    "box", "prompt", "optin", "cmp", "gate", "toast", "footer", "header",
})
_PRIVACY_QUALIFIERS = frozenset({"banner", "modal", "popup", "overlay", "consent", "choices", "toast", "bar"})
_COMMENT_QUALIFIERS = frozenset({
    "section", "list", "box", "form", "thread", "area", "wrapper", "container",
    "respond", "reply", "replies", "widget", "panel", "feed", "block", "drawer", "stream",
    "user", "reader", "article", "post",
})
_NEWSLETTER_QUALIFIERS = frozenset({
    "signup", "subscribe", "subscription", "form", "box", "widget", "banner", "modal",
    "popup", "overlay", "card", "bar", "cta", "module", "promo", "block", "container",
    "wrapper", "section", "footer", "sidebar", "prompt", "optin",
})
_RELATED_NOUNS = frozenset({
    "articles", "article", "posts", "post", "stories", "story", "news", "links",
    "reads", "content", "topics", "items", "widget", "box", "sidebar", "cards",
})
_TRENDING_NOUNS = frozenset({"articles", "posts", "stories", "news", "tickers", "topics", "now", "widget", "box", "sidebar", "list"})
_MORE_NOUNS = frozenset({"articles", "stories", "news", "posts", "reads"})
_SHARE_NOUNS = frozenset({"buttons", "bar", "links", "widget", "box", "icons", "tools", "panel"})
_AD_INDEXED_RE = re.compile(r"^ad(?:[-_]\d+)?$")
_CAMEL_BOUNDARY_RE = re.compile(r"([a-z0-9])([A-Z])")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_HTML_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SPA_EMPTY_ROOT_RE = re.compile(
    r"""<(?:div|main)\b[^>]*\b(?:data-reactroot(?:\s*=\s*(?:["'][^"']*["']|[^\s>]+))?|id\s*=\s*["']?(?:root|app|__next|__nuxt|svelte|mount)["']?)[^>]*>\s*(?:<!--.*?-->\s*|loading\.{0,3}|please wait\.{0,3})?\s*</(?:div|main)>""",
    re.IGNORECASE | re.DOTALL,
)
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)
_NOSCRIPT_BLOCK_RE = re.compile(r"<noscript\b[^>]*>(.*?)</noscript>", re.IGNORECASE | re.DOTALL)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")

_CHALLENGE_HTML_MARKERS = (
    "_cf_chl_opt",
    "cf-chl-widget",
    "cf-turnstile",
    "/cdn-cgi/challenge-platform/",
    "cf-browser-verification",
    "cf_chl_prog",
    'id="challenge-running"',
    "id='challenge-running'",
    'id="challenge-form"',
    "id='challenge-form'",
    'id="cf-please-wait"',
    "id='cf-please-wait'",
    "px-captcha",
    "captcha-delivery.com",
)
_CHALLENGE_TITLE_MARKERS = (
    "just a moment...",
    "attention required! | cloudflare",
    "pardon our interruption",
    "verify you are human",
    "human verification",
    "robot or human?",
    "ddos-guard",
)
_CHALLENGE_TEXT_MARKERS = (
    "checking if the site connection is secure",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "performing security verification",
    "please verify you are a human",
    "verify you are human",
    "access to this page has been denied",
    "please complete the security check",
)
_PAYWALL_TEXT_MARKERS = (
    "subscribe to continue reading",
    "subscribe to read the full",
    "subscribe to read this article",
    "subscribe to unlock",
    "sign in to continue reading",
    "log in to continue reading",
    "login to continue reading",
    "create a free account to continue reading",
    "register to continue reading",
    "you have reached your article limit",
    "reached your free article limit",
    "reached your monthly article limit",
    "this content is for subscribers only",
    "this article is for subscribers only",
    "exclusively for subscribers",
    "unlock this article",
    "unlock full access",
    "become a member to read",
    "join to read the full",
)
_PAYWALL_HTML_MARKERS = (
    '"isaccessibleforfree":false',
    '"isaccessibleforfree": false',
    "'isaccessibleforfree':false",
    "'isaccessibleforfree': false",
    "paywall-overlay",
    "paywall-modal",
    "paywall-wall",
    "piano-offer",
    "tp-modal",
    "regwall",
    "meter-wall",
)
_JS_REQUIRED_MARKERS = (
    "enable javascript",
    "javascript is required",
    "javascript is disabled",
    "turn on javascript",
    "requires javascript",
    "please activate javascript",
    "you need to enable javascript",
    "browser does not support javascript",
    "without javascript enabled",
)
_LOADING_PLACEHOLDER_WORDS = frozenset({
    "", "loading", "loading...", "please wait", "please wait...", "initializing", "initializing...", "redirecting", "redirecting...",
})
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


@dataclass
class _DensityFrame:
    scope_index: int
    part_start: int
    has_leading_newline: bool = False
    total_chars: int = 0
    link_chars: int = 0
    link_count: int = 0


def _split_attr_subtokens(item: str) -> list[str]:
    expanded = _CAMEL_BOUNDARY_RE.sub(r"\1 \2", item).lower()
    return [tok for tok in _NON_ALNUM_RE.split(expanded) if tok]


def _is_boilerplate_attr_item(item: str) -> bool:
    lowered = item.strip().lower()
    if not lowered:
        return False
    if lowered in _EXACT_BOILERPLATE_ATTRS or _AD_INDEXED_RE.fullmatch(lowered):
        return True
    subtokens = _split_attr_subtokens(item)
    if not subtokens:
        return False
    if any(tok in _BOILERPLATE_SUBTOKENS for tok in subtokens):
        return True
    subtoken_set = set(subtokens)
    if "ad" in subtoken_set and any(tok in _AD_QUALIFIERS for tok in subtokens):
        return True
    if "sponsored" in subtoken_set and (len(subtokens) == 1 or any(tok in _SPONSORED_QUALIFIERS for tok in subtokens)):
        return True
    if ("cookie" in subtoken_set or "cookies" in subtoken_set or "gdpr" in subtoken_set or "consent" in subtoken_set) and (
        len(subtokens) == 1 or any(tok in _COOKIE_QUALIFIERS for tok in subtokens)
    ):
        return True
    if "privacy" in subtoken_set and any(tok in _PRIVACY_QUALIFIERS for tok in subtokens):
        return True
    if ("comment" in subtoken_set or "comments" in subtoken_set) and (
        ("comments" in subtoken_set and len(subtokens) == 1)
        or any(tok in _COMMENT_QUALIFIERS for tok in subtokens)
    ):
        return True
    if ("newsletter" in subtoken_set or "newsletters" in subtoken_set) and any(tok in _NEWSLETTER_QUALIFIERS for tok in subtokens):
        return True
    for left, right in zip(subtokens, subtokens[1:]):
        if left == "related" and right in _RELATED_NOUNS:
            return True
        if left == "recommended" and right in _RELATED_NOUNS:
            return True
        if left == "trending" and right in _TRENDING_NOUNS:
            return True
        if left == "more" and right in _MORE_NOUNS:
            return True
        if left in {"share", "social"} and right in _SHARE_NOUNS:
            return True
        if left in {"subscribe", "email"} and right in {"box", "banner", "widget", "signup", "form", "modal", "popup"}:
            return True
    return False


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
        self._scope_stack: list[tuple[str, bool, bool]] = []
        self._density_stack: list[_DensityFrame] = []

    def _boilerplate_suppressed(self) -> bool:
        return bool(self._scope_stack and self._scope_stack[-1][1])

    def _in_link(self) -> bool:
        return bool(self._scope_stack and self._scope_stack[-1][2])

    def can_flush_parts(self) -> bool:
        return not self._row_stack and not self._cell_stack and not self._density_stack

    def _retire_density_frames(self) -> None:
        self._density_stack.clear()

    @staticmethod
    def _has_boilerplate_role(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name == "role" and isinstance(value, str):
                if any(token in _BOILERPLATE_ROLES for token in value.strip().lower().split()):
                    return True
        return False

    @staticmethod
    def _has_preserved_role(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name == "role" and isinstance(value, str):
                if any(token in _PRESERVE_ROLES for token in value.strip().lower().split()):
                    return True
        return False

    @staticmethod
    def _has_boilerplate_attrs(attrs: list[tuple[str, str | None]]) -> bool:
        for name, value in attrs:
            if name in {"class", "id"} and isinstance(value, str):
                for item in value.split():
                    if _is_boilerplate_attr_item(item):
                        return True
        return False

    @staticmethod
    def _should_prune_density_frame(frame: _DensityFrame) -> bool:
        if frame.total_chars < 6:
            return False
        ratio = frame.link_chars / frame.total_chars
        if frame.link_count >= 3 and ratio >= 0.70:
            return True
        if frame.link_count >= 4 and frame.total_chars >= 12 and ratio >= 0.65:
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
        preserved_role = self._has_preserved_role(attrs)
        if tag in _PRESERVE_CONTAINER_TAGS or preserved_role:
            suppressed = False
            self._retire_density_frames()
        elif tag in _BOILERPLATE_TAGS or self._has_boilerplate_role(attrs):
            suppressed = True
        elif not self._boilerplate_suppressed() and self._table_depth == 0 and self._has_boilerplate_attrs(attrs):
            suppressed = True
        else:
            suppressed = self._boilerplate_suppressed()
        has_href = tag == "a" and any(name == "href" and value is not None for name, value in attrs)
        parent_in_link = self._in_link()
        in_link = (has_href or parent_in_link) and tag not in _PRESERVE_CONTAINER_TAGS and not preserved_role and not suppressed
        if has_href and not parent_in_link and not suppressed and self._density_stack:
            self._density_stack[-1].link_count += 1
        scope_index = len(self._scope_stack)
        if tag not in _VOID_HTML_TAGS:
            self._scope_stack.append((tag, suppressed, in_link))
        if suppressed:
            return
        if tag in _DENSITY_CANDIDATE_TAGS and self._table_depth == 0 and not preserved_role and tag not in _VOID_HTML_TAGS:
            self._density_stack.append(_DensityFrame(
                scope_index=scope_index,
                part_start=len(self.parts),
                has_leading_newline=(tag == "div"),
            ))
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
        pruned_closing = False
        if scope_idx is not None:
            was_suppressed = self._scope_stack[scope_idx][1]
            del self._scope_stack[scope_idx:]
            while self._density_stack and self._density_stack[-1].scope_index >= scope_idx:
                frame = self._density_stack.pop()
                if self._should_prune_density_frame(frame):
                    del self.parts[frame.part_start:]
                    if frame.scope_index == scope_idx and frame.has_leading_newline:
                        pruned_closing = True
                elif self._density_stack:
                    parent = self._density_stack[-1]
                    parent.total_chars += frame.total_chars
                    parent.link_chars += frame.link_chars
                    parent.link_count += frame.link_count
            if was_suppressed or pruned_closing:
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
        if self._density_stack:
            non_ws = sum(1 for ch in data if not ch.isspace())
            if non_ws:
                frame = self._density_stack[-1]
                frame.total_chars += non_ws
                if self._in_link():
                    frame.link_chars += non_ws
            for idx in range(len(self._density_stack) - 1, -1, -1):
                cur = self._density_stack[idx]
                if cur.total_chars > _MAX_DENSITY_BUFFER_CHARS or len(self.parts) - cur.part_start > _MAX_DENSITY_BUFFER_PARTS:
                    del self._density_stack[:idx + 1]
                    break

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
        self._density_stack.clear()


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
class SoftBlockMatch:
    reason: str
    marker: str
    detail: str


_SOFT_BLOCK_HEAD_BYTES = 131_072


def _extract_html_title(html: str) -> str | None:
    match = _HTML_TITLE_RE.search(html or "")
    if not match:
        return None
    cleaned = re.sub(r"\s+", " ", html_module.unescape(_TAG_STRIP_RE.sub(" ", match.group(1)))).strip()
    return cleaned or None


def _html_head_sample(source: Path | bytes | str, content_type_header: str) -> str:
    if isinstance(source, str):
        return source[:_SOFT_BLOCK_HEAD_BYTES]
    if isinstance(source, bytes):
        head = source[:_SOFT_BLOCK_HEAD_BYTES]
    else:
        try:
            with source.open("rb") as handle:
                head = handle.read(_SOFT_BLOCK_HEAD_BYTES)
        except OSError:
            return ""
    return _decode_text_bytes(head, content_type_header, is_html=True)


def detect_soft_block(html: str, extracted_text: str, title: str | None = None) -> SoftBlockMatch | None:
    """Detect conservative HTTP-200 anti-bot challenges, soft paywalls, and JS-required empty shells.

    Requires minimal real extracted prose combined with clear challenge, paywall, or JS-shell
    markers so legitimate short press releases, filings, and articles mentioning those topics are preserved.
    """
    html_str = html or ""
    text_str = extracted_text or ""
    stripped_text = text_str.strip()
    char_count = len(stripped_text)
    word_count = len(_WORD_TOKEN_RE.findall(stripped_text))

    if char_count >= 1500 or word_count >= 220:
        return None

    html_lower = html_str.lower()
    text_lower = stripped_text.lower()
    html_title = _extract_html_title(html_str)
    title_candidates = [t.strip().lower() for t in (title, html_title) if isinstance(t, str) and t.strip()]

    if word_count < 180 and char_count < 1200:
        if any(marker in html_lower for marker in _CHALLENGE_HTML_MARKERS):
            return SoftBlockMatch(
                reason="fetch_blocked_soft_wall",
                marker="challenge_html",
                detail="Page returned an anti-bot or browser challenge interstitial instead of document content.",
            )
        if any(marker in t for t in title_candidates for marker in _CHALLENGE_TITLE_MARKERS):
            return SoftBlockMatch(
                reason="fetch_blocked_soft_wall",
                marker="challenge_title",
                detail="Page returned an anti-bot or security challenge title instead of document content.",
            )
        if any(marker in text_lower for marker in _CHALLENGE_TEXT_MARKERS) or (
            "enable javascript and cookies to continue" in html_lower
        ):
            return SoftBlockMatch(
                reason="fetch_blocked_soft_wall",
                marker="challenge_text",
                detail="Page returned a challenge prompt requiring browser verification.",
            )
        if ("why have i been blocked" in text_lower or "attention required" in text_lower) and "cloudflare" in (
            text_lower + " " + " ".join(title_candidates)
        ):
            return SoftBlockMatch(
                reason="fetch_blocked_soft_wall",
                marker="cloudflare_block",
                detail="Page returned a Cloudflare block notice instead of document content.",
            )
        if "datadome" in html_lower and "captcha" in (html_lower + " " + text_lower):
            return SoftBlockMatch(
                reason="fetch_blocked_soft_wall",
                marker="datadome_challenge",
                detail="Page returned a DataDome captcha interstitial.",
            )

    if any(marker in text_lower or marker in html_lower for marker in _PAYWALL_TEXT_MARKERS) or (
        "already a subscriber" in text_lower and ("sign in" in text_lower or "log in" in text_lower)
    ) or any(marker in html_lower for marker in _PAYWALL_HTML_MARKERS):
        return SoftBlockMatch(
            reason="fetch_blocked_soft_wall",
            marker="soft_paywall",
            detail="Page returned a soft paywall or subscriber-only gate with minimal readable content.",
        )

    if word_count < 40 and char_count < 250:
        noscript_text = " ".join(
            _TAG_STRIP_RE.sub(" ", block).lower() for block in _NOSCRIPT_BLOCK_RE.findall(html_str)
        )
        js_haystack = f"{text_lower}\n{noscript_text}\n{' '.join(title_candidates)}"
        if any(marker in js_haystack for marker in _JS_REQUIRED_MARKERS):
            return SoftBlockMatch(
                reason="js_rendered_empty",
                marker="enable_js_prompt",
                detail="Page requires JavaScript execution and contains no usable server-rendered prose.",
            )
        if text_lower in _LOADING_PLACEHOLDER_WORDS and _SCRIPT_TAG_RE.search(html_str) and _SPA_EMPTY_ROOT_RE.search(html_str):
            return SoftBlockMatch(
                reason="js_rendered_empty",
                marker="empty_spa_shell",
                detail="Page returned an empty JavaScript application shell with no server-rendered content.",
            )

    return None


@dataclass(frozen=True)
class FetchedText:
    text: str
    response_sha256: str
    content_type: str
    original_html_base64: str | None = None
    pages: dict | None = None
    published_date: dict | None = None


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
    published_date: dict | None = None


# Publication metadata (JSON-LD, meta tags, <time>, bylines) sits in the head or near the top of the page, so only the
# first bytes are parsed: a multi-megabyte HTML file never goes through the date extractor in full.
PUBLISHED_DATE_HEAD_BYTES = 524_288


def _published_date_info(raw: bytes | None, path: Path | None) -> dict | None:
    from .news.published_date import extract_published_date
    try:
        if raw is None and path is not None:
            with path.open("rb") as handle:
                raw = handle.read(PUBLISHED_DATE_HEAD_BYTES)
        if not raw:
            return None
        found = extract_published_date(raw[:PUBLISHED_DATE_HEAD_BYTES])
    except Exception:
        return None
    if found.published is None and found.modified is None:
        return None
    info: dict = {}
    if found.published is not None:
        info.update(published_at=found.published.iso(), precision=found.published.precision, timezone=found.published.timezone,
                    source=found.published.source, confidence=found.published.confidence)
    if found.modified is not None:
        info["modified_at"] = found.modified.iso()
    return info


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
            if parser.parts and parser.can_flush_parts():
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
    terms = terms_decision(url)
    if not terms.allowed:
        raise FetchFailure("source_blocked_terms", terms.message)
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
    if any(s in OCR_CANDIDATE_STATUSES for s in statuses):
        with tempfile.TemporaryDirectory(prefix="foresight-ocr-") as directory:
            path = Path(directory) / "source.pdf"
            path.write_bytes(raw)
            text, document_pages = _ingest_ocr(path, text, document_pages, title=request.title, url=final_url,
                                               usage=ocr_usage)
    if not any(entry[2] in TEXT_STATUSES for entry in document_pages["pages"]):
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
                usage=None, max_pages_override: int | None = None,
                seconds_override: float | None = None) -> tuple[str, dict]:
    """OCR prioritized OCR-candidate pages within the ingestion budget; unchanged when no engine is available.

    The vision engine runs only through ``usage`` hooks (reserve/settle of the research run); without them
    only local engines are used.
    """
    from .research_ocr import choose_ocr_languages, default_engines, ocr_pdf_pages

    max_pages = max_pages_override if max_pages_override is not None else int(_env_int("FORESIGHTNINE_OCR_INGEST_MAX_PAGES", 4))
    pending = prioritize_ocr_pages(text, document_pages, max_pages=max_pages)
    engines = default_engines()
    if not pending or not engines:
        return text, document_pages
    langs = choose_ocr_languages(title=title, url=url, sample_text=text[:4000])
    budget_sec = seconds_override if seconds_override is not None else float(_env_int("FORESIGHTNINE_OCR_INGEST_SECONDS", 25))
    deadline = monotonic() + budget_sec
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
                       ocr_usage=None, text_order: str = "layout") -> FetchedDocument:
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise FetchFailure("source_invalid", "Invalid PDF header.")
    try:
        result = extract_pdf_pages(path, deadline=monotonic() + pdf_extract_budget_seconds(), text_order=text_order)
    except PdfPagesError as error:
        if error.code == "pdf_extractor_unavailable":
            raise FetchFailure("source_unavailable", "pdf_extractor_unavailable") from error
        if error.code == "pdf_timeout":
            raise FetchFailure("source_invalid", "PDF text extraction timed out locally.") from error
        raise FetchFailure("source_invalid", "PDF extraction failed.") from error
    text, document_pages = result.text, result.document_pages
    statuses = [entry[2] for entry in document_pages["pages"]]
    if "failed" in statuses and not any(s in TEXT_STATUSES for s in statuses):
        raise FetchFailure("source_invalid", "PDF page extraction did not complete (local timeout or failure).")
    if any(s in OCR_CANDIDATE_STATUSES for s in statuses):
        text, document_pages = _ingest_ocr(path, text, document_pages,
                                           title=getattr(request, "title", None), url=final_url, usage=ocr_usage)
    if not any(entry[2] in TEXT_STATUSES for entry in document_pages["pages"]):
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
    html_sample = _html_head_sample(source, content_type_header) if is_html else ""
    html_title = _extract_html_title(html_sample) if is_html else None
    try:
        text, document_pages = extract_text_pages(source, content_type_header, is_html=is_html, deadline=parse_deadline)
    except FetchFailure as error:
        if is_html and error.code == "source_invalid":
            soft_block = detect_soft_block(html_sample, "", html_title)
            if soft_block is not None:
                raise FetchFailure(soft_block.reason, soft_block.detail) from error
        raise
    if is_html and len(text) < 2000:
        soft_block = detect_soft_block(html_sample, text, html_title)
        if soft_block is not None:
            raise FetchFailure(soft_block.reason, soft_block.detail)
    original_base64: str | None = None
    date_info = _published_date_info(raw, spool_path) if is_html else None
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
        published_date=date_info,
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
        published_date=document.published_date,
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
