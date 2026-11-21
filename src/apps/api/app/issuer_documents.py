"""``issuer_documents``: find an issuer's own website and list its investor-relations document leads.

HKEX Listing Rule 2.07C(6) obliges Hong Kong issuers to publish announcements on their own website at the same time
and keep them for at least five years; many A-share issuers also publish on their investor-relations (IR) sites. This is
the terms-compliant automated route where HKEXnews and cninfo are not.

Flow: resolve the official website (Wikidata, ``entity_web``; or a URL already known in the run) -> robots.txt (own
parser, agent token and ``*`` groups, Allow/Disallow with wildcards, Crawl-delay) -> bounded crawl of the home page plus
IR-looking pages -> ranked document leads (title, url, date, type guess, language). Leads are leads, never evidence;
they are acquired later with ``acquire_sources``. Nothing here fetches a host that ``research_source_terms`` blocks, all
requests are read-only GETs, and an unknown outcome is recorded rather than replayed.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .entity_web import ATTRIBUTION as WIKIDATA_ATTRIBUTION, EXCHANGE_MARKET, IssuerProfile, WikidataClient, normalize_ticker
from .news.published_date import find_text_date
from .research_fetch import fetch_allowed_hosts, is_host_blocked, is_sec_host, validate_public_https_url
from .research_source_terms import terms_decision

DocType = Literal['announcement', 'annual_report', 'interim_report', 'circular', 'results']
Market = Literal['US', 'CN', 'HK', 'JP', 'KR', 'TW', 'GB', 'IN', 'CA', 'AU', 'DE']
AGENT_TOKEN = 'foresightnine'
CRAWL_USER_AGENT = 'ForesightNine-Research/1.0 (financial research tool; honors robots.txt)'
MAX_PAGES_ENV = 'FORESIGHTNINE_ISSUER_CRAWL_MAX_PAGES'
SECONDS_ENV = 'FORESIGHTNINE_ISSUER_CRAWL_SECONDS'
DELAY_ENV = 'FORESIGHTNINE_ISSUER_CRAWL_DELAY_SECONDS'
DEFAULT_MAX_PAGES = 12
HARD_MAX_PAGES = 40
DEFAULT_SECONDS = 45.0
DEFAULT_DELAY = 1.0
MAX_CRAWL_DELAY = 10.0
MAX_PAGE_BYTES = 1_500_000
MAX_ROBOTS_BYTES = 500_000
MAX_REDIRECTS = 3
MAX_DEPTH = 2
MAX_ANCHORS = 3000
ROBOTS_TTL = 24 * 3600
ROBOTS_FAILURE_TTL = 600
DOC_TYPES: tuple[str, ...] = ('announcement', 'annual_report', 'interim_report', 'circular', 'results')
_PUBLIC_SUFFIX_2 = frozenset({'co.uk', 'org.uk', 'com.cn', 'net.cn', 'org.cn', 'gov.cn', 'edu.cn', 'com.hk', 'org.hk',
                              'edu.hk', 'com.tw', 'org.tw', 'co.jp', 'or.jp', 'co.kr', 'or.kr', 'com.au', 'co.in',
                              'com.sg', 'co.nz', 'com.br', 'co.za'})


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, ''))
    except ValueError:
        return default
    return value if low <= value <= high else default


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, ''))
    except ValueError:
        return default
    return value if low <= value <= high else default


class IssuerDocumentsRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    entity_name: str | None = Field(default=None, max_length=120, description='Company name in any language, as the user wrote it.')
    entity_id: str | None = Field(default=None, max_length=64, description='entity_id returned by resolve_entity.')
    ticker: str | None = Field(default=None, max_length=16, description='Listing ticker such as 0700 or 600519; combine with market.')
    market: Market | None = Field(default=None, description='Listing market, narrows the match.')
    website_url: str | None = Field(default=None, max_length=300, description=(
        "The issuer's official website. Only accepted when it is a host already known in this run (a URL the user wrote, "
        'a lead, or a candidate returned by an earlier issuer_documents call).'))
    since: date | None = Field(default=None, description='Optional earliest document date (YYYY-MM-DD); undated links are kept.')
    until: date | None = Field(default=None, description='Optional latest document date (YYYY-MM-DD).')
    doc_types: list[DocType] | None = Field(default=None, max_length=5, description=(
        'Optional types to prefer: announcement, annual_report, interim_report, circular, results.'))
    limit: int = Field(default=20, ge=1, le=50, description='Maximum number of document leads (1-50).')
    rationale: str = Field(min_length=1)

    @model_validator(mode='after')
    def valid(self) -> 'IssuerDocumentsRequest':
        if not (self.entity_name or self.entity_id or self.ticker or self.website_url):
            raise ValueError('Give entity_name, entity_id, ticker or website_url.')
        if self.since and self.until and self.since > self.until:
            raise ValueError('since is after until.')
        return self


# ---------------------------------------------------------------------------------------------- robots.txt

@dataclass
class RobotsPolicy:
    allow_all: bool = False
    disallow_all: bool = False
    rules: list[tuple[bool, re.Pattern[str], int]] = field(default_factory=list)   # (allow, pattern, specificity)
    crawl_delay: float | None = None

    def can_fetch(self, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all or not self.rules:
            return True
        parts = urlsplit(url)
        target = (parts.path or '/') + (('?' + parts.query) if parts.query else '')
        best: tuple[int, bool] | None = None
        for allow, pattern, specificity in self.rules:
            if pattern.match(target) and (best is None or specificity > best[0] or (specificity == best[0] and allow)):
                best = (specificity, allow)
        return True if best is None else best[1]


def _robots_pattern(value: str) -> re.Pattern[str]:
    anchored = value.endswith('$')
    body = value[:-1] if anchored else value
    regex = ''.join('.*' if ch == '*' else re.escape(ch) for ch in body)
    return re.compile(regex + ('$' if anchored else ''))


def parse_robots(text: str, agent_token: str = AGENT_TOKEN) -> RobotsPolicy:
    """Parse robots.txt for ``agent_token``: its own group(s) when present, otherwise the ``*`` group(s)."""
    token = agent_token.lower()
    groups: list[tuple[list[str], list[tuple[str, str]]]] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []
    reading_agents = False
    for raw in (text or '').splitlines()[:5000]:
        line = raw.split('#', 1)[0].strip()
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key, value = key.strip().lower(), value.strip()
        if key == 'user-agent':
            if not reading_agents:
                if agents:
                    groups.append((agents, rules))
                agents, rules = [], []
            agents.append(value.lower())
            reading_agents = True
        elif key in ('allow', 'disallow', 'crawl-delay'):
            reading_agents = False
            if agents:
                rules.append((key, value))
    if agents:
        groups.append((agents, rules))
    specific = [g for g in groups if any(a != '*' and a and (a == token or token.startswith(a) or a in token) for a in g[0])]
    chosen = specific or [g for g in groups if '*' in g[0]]
    policy = RobotsPolicy()
    for _, group_rules in chosen:
        for key, value in group_rules:
            if key == 'crawl-delay':
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay >= 0:
                    policy.crawl_delay = max(policy.crawl_delay or 0.0, delay)
            elif value:
                policy.rules.append((key == 'allow', _robots_pattern(unquote(value) if '%' in value else value), len(value.rstrip('$'))))
    if not policy.rules:
        policy.allow_all = True
    return policy


_ROBOTS_CACHE: dict[str, tuple[float, RobotsPolicy]] = {}


def clear_robots_cache() -> None:
    _ROBOTS_CACHE.clear()


# ---------------------------------------------------------------------------------------------- HTML links

@dataclass
class Anchor:
    url: str
    text: str
    context: str = ''
    rel_next: bool = False


_BLOCK_TAGS = frozenset({'li', 'tr', 'p', 'dd', 'dt', 'article', 'section'})
_SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'svg', 'template'})


class _LinkParser(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.anchors: list[Anchor] = []
        self._stack: list[dict[str, Any]] = []
        self._current: tuple[Anchor, list[str]] | None = None
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag in _BLOCK_TAGS:
            self._stack.append({'tag': tag, 'text': [], 'anchors': []})
        elif tag == 'a' and len(self.anchors) < MAX_ANCHORS:
            values = {k.lower(): (v or '') for k, v in attrs}
            href = values.get('href', '').strip()
            if href and not href.lower().startswith(('javascript:', 'mailto:', 'tel:', '#', 'data:')):
                try:
                    absolute = urljoin(self.base, href)
                except ValueError:
                    return
                anchor = Anchor(absolute, '', rel_next='next' in values.get('rel', '').lower().split())
                self._current = (anchor, [])
                if values.get('title'):
                    self._current[1].append(values['title'])
                self.anchors.append(anchor)
                if self._stack:
                    self._stack[-1]['anchors'].append(anchor)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if tag == 'a' and self._current is not None:
            anchor, parts = self._current
            anchor.text = _squash(' '.join(parts))[:300] or anchor.text
            self._current = None
        elif tag in _BLOCK_TAGS and self._stack and self._stack[-1]['tag'] == tag:
            block = self._stack.pop()
            text = _squash(' '.join(block['text']))
            if len(text) <= 400:
                for anchor in block['anchors']:
                    if not anchor.context:
                        anchor.context = text
            if self._stack:
                self._stack[-1]['anchors'].extend(block['anchors'])
                self._stack[-1]['text'].append(text)

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._current is not None:
            self._current[1].append(data)
        if self._stack:
            self._stack[-1]['text'].append(data)


def _squash(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def extract_anchors(html: str, base_url: str) -> list[Anchor]:
    parser = _LinkParser(base_url)
    try:
        parser.feed(html[:MAX_PAGE_BYTES])
        parser.close()
    except (ValueError, RecursionError):
        pass
    return parser.anchors


# ---------------------------------------------------------------------------------------------- classification

_IR_RE = re.compile(
    r'investor|/ir(?:[/._-]|$)|\bir\b|announcement|disclosure|circular|financial|report|results|shareholder|stock-info|'
    r'publication|regulatory|corporate-?governance|公告|公佈|公布|投资者|投資者|股东|股東|通函|年报|年報|中期|业绩|業績|財務|财务|信息披露|資訊披露|披露',
    re.I)
_NEXT_RE = re.compile(r'^(?:next|more|older|下一页|下一頁|更多|›|»|>)$', re.I)
_GENERIC_TITLE = re.compile(r'^(?:pdf|download|下载|下載|查看|view|more|read more|详情|詳情|点击下载|點擊下載|\d+(?:\.\d+)?\s?[km]b)$', re.I)
_TYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('annual_report', re.compile(r'annual[\s_-]*report|年度报告|年度報告|年报|年報|\b10-k\b|\b20-f\b|annual[\s_-]*results', re.I)),
    ('interim_report', re.compile(r'interim[\s_-]*report|half[\s_-]*year|semi[\s_-]*annual|中期报告|中期報告|半年度|半年报|半年報|中报|中期', re.I)),
    ('circular', re.compile(r'circular|通函', re.I)),
    ('results', re.compile(r'results|earnings|quarter|业绩|業績|季度|季报|季報|财务报告|財務報告|financial[\s_-]*(?:report|statements?)|presentation', re.I)),
    ('announcement', re.compile(r'announcement|notice|press[\s_-]*release|公告|公佈|公布|通知|披露|海外监管|海外監管', re.I)),
)
_DOC_EXT = ('.pdf',)
_HTML_EXT = ('.htm', '.html', '.shtml', '.shtm', '.php', '.aspx', '.asp', '')
_URL_DATE = re.compile(r'(?<!\d)((?:19|20)\d{2})[-/_.]?(0[1-9]|1[0-2])[-/_.]?(0[1-9]|[12]\d|3[01])(?!\d)')
_CJK = re.compile(r'[㐀-鿿]')


def guess_doc_type(*texts: str) -> str:
    joined = ' '.join(t for t in texts if t)
    for doc_type, pattern in _TYPE_PATTERNS:
        if pattern.search(joined):
            return doc_type
    return 'other'


def _guess_language(text: str) -> str | None:
    letters = [c for c in text if c.isalpha()]
    return 'zh' if letters and len(_CJK.findall(text)) / len(letters) >= 0.3 else ('en' if letters else None)


def registrable_domain(host: str) -> str:
    labels = host.lower().rstrip('.').split('.')
    if len(labels) <= 2:
        return '.'.join(labels)
    return '.'.join(labels[-3:]) if '.'.join(labels[-2:]) in _PUBLIC_SUFFIX_2 else '.'.join(labels[-2:])


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))


def _document_date(anchor: Anchor, today: date) -> date | None:
    limit = today + timedelta(days=2)
    for text, source in ((anchor.text, 'anchor'), (anchor.context, 'context')):
        found = find_text_date(text, source, 'medium') if text else None
        if found is not None and 1990 <= found.value.year and found.value.date() <= limit:
            return found.value.date()
    match = _URL_DATE.search(unquote(anchor.url))
    if match:
        try:
            value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
        return value if value <= limit else None
    return None


def _title_for(anchor: Anchor, url: str) -> str:
    title = anchor.text.strip()
    if not title or _GENERIC_TITLE.match(title):
        context = anchor.context.strip()
        title = context if context and not _GENERIC_TITLE.match(context) else unquote(urlsplit(url).path.rsplit('/', 1)[-1]) or url
    return title[:200]


# ---------------------------------------------------------------------------------------------- crawl

@dataclass
class _Response:
    status: int
    body: bytes
    headers: Mapping[str, str]
    final_url: str


class _Blocked(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class IssuerCrawler:
    """Bounded, polite, read-only fetcher shared by the robots check and the page crawl."""

    def __init__(self, *, transport: Callable[..., Any] | None = None, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep, deadline: float | None = None,
                 delay_seconds: float | None = None, max_pages: int | None = None,
                 ensure_running: Callable[[], None] = lambda: None, wall_clock: Callable[[], float] = time.time) -> None:
        if transport is None:
            from .openai_compatible import _default_binary_transport
            transport = _default_binary_transport
        self._transport = transport
        self._clock, self._sleeper, self._wall = clock, sleeper, wall_clock
        self._deadline = deadline
        self._delay = _env_float(DELAY_ENV, DEFAULT_DELAY, 0.0, 30.0) if delay_seconds is None else max(0.0, delay_seconds)
        self.max_pages = max_pages if max_pages is not None else _env_int(MAX_PAGES_ENV, DEFAULT_MAX_PAGES, 1, HARD_MAX_PAGES)
        self._ensure_running = ensure_running
        self._last_request: dict[str, float] = {}
        self._policies: dict[str, RobotsPolicy] = {}
        self.requests = 0
        self.time_exhausted = False

    def remaining(self) -> float:
        return float('inf') if self._deadline is None else self._deadline - self._clock()

    def check_url(self, url: str) -> str:
        """Host after safety checks; raises ``_Blocked`` for blocked, SEC, unsafe or non-allowlisted targets."""
        try:
            host = validate_public_https_url(url, allow_fragment=False, check_sec_path=False)
        except ValueError:
            raise _Blocked('invalid_url') from None
        if is_sec_host(host):
            raise _Blocked('sec_host')
        if is_host_blocked(host):
            raise _Blocked('source_blocked_policy')
        decision = terms_decision(url)
        if not decision.allowed:
            raise _Blocked('source_blocked_terms')
        allowed = fetch_allowed_hosts()
        if allowed is not None and host not in allowed:
            raise _Blocked('source_blocked_policy')
        return host

    def _pace(self, host: str, extra_delay: float | None) -> None:
        delay = max(self._delay, min(extra_delay or 0.0, MAX_CRAWL_DELAY))
        wait = self._last_request.get(host, float('-inf')) + delay - self._clock()
        if wait > 0:
            if wait >= self.remaining():
                self.time_exhausted = True
                raise _Blocked('time_budget')
            self._sleeper(wait)
        self._last_request[host] = self._clock()

    def _get(self, url: str, *, max_bytes: int, accept: str, crawl_delay: float | None = None) -> _Response:
        """One GET with manual, re-checked redirects. Raises ``_Blocked`` or ``OSError`` (outcome unknown)."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host = self.check_url(current)
            self._ensure_running()
            remaining = self.remaining()
            if remaining <= 0.5:
                self.time_exhausted = True
                raise _Blocked('time_budget')
            self._pace(host, crawl_delay)
            self.requests += 1
            response = self._transport('GET', current, {'Accept': accept, 'User-Agent': CRAWL_USER_AGENT}, b'',
                                       min(10.0, max(1.0, self.remaining())), max_response_bytes=max_bytes)
            status = int(getattr(response, 'status_code', 0))
            headers = {str(k).lower(): str(v) for k, v in (getattr(response, 'headers', None) or {}).items()}
            if status in (301, 302, 303, 307, 308) and headers.get('location'):
                current = _canonical(urljoin(current, headers['location'].strip()))
                continue
            body = response.body if isinstance(response.body, bytes) else str(response.body or '').encode('utf-8')
            return _Response(status, body[:max_bytes], headers, current)
        raise OSError('too many redirects')

    def policy(self, url: str) -> RobotsPolicy:
        host = urlsplit(url).hostname or ''
        if host in self._policies:
            return self._policies[host]
        cached = _ROBOTS_CACHE.get(host)
        if cached is not None and cached[0] > self._wall():
            self._policies[host] = cached[1]
            return cached[1]
        ttl = ROBOTS_TTL
        try:
            response = self._get(f'https://{host}/robots.txt', max_bytes=MAX_ROBOTS_BYTES, accept='text/plain, */*;q=0.1')
            if response.status == 200:
                policy = parse_robots(response.body.decode('utf-8', errors='replace'))
            elif response.status in (401, 403) or response.status == 429 or response.status >= 500:
                policy, ttl = RobotsPolicy(disallow_all=True), ROBOTS_FAILURE_TTL    # refusal or unreachable: do not crawl
            else:
                policy = RobotsPolicy(allow_all=True)                                   # 404/410 etc.: no restrictions
        except _Blocked as error:
            if error.code == 'time_budget':
                raise
            policy, ttl = RobotsPolicy(disallow_all=True), ROBOTS_FAILURE_TTL
        except OSError:
            policy, ttl = RobotsPolicy(disallow_all=True), ROBOTS_FAILURE_TTL
        except Exception:
            policy, ttl = RobotsPolicy(disallow_all=True), ROBOTS_FAILURE_TTL
        self._policies[host] = policy
        if len(_ROBOTS_CACHE) > 512:
            _ROBOTS_CACHE.clear()
        _ROBOTS_CACHE[host] = (self._wall() + ttl, policy)
        return policy

    def fetch_page(self, url: str) -> _Response:
        self.check_url(url)
        policy = self.policy(url)
        if not policy.can_fetch(url):
            raise _Blocked('robots_disallow')
        response = self._get(url, max_bytes=MAX_PAGE_BYTES, accept='text/html, application/xhtml+xml;q=0.9',
                             crawl_delay=policy.crawl_delay)
        final_policy = self.policy(response.final_url) if urlsplit(response.final_url).hostname != urlsplit(url).hostname else policy
        if not final_policy.can_fetch(response.final_url):
            raise _Blocked('robots_disallow')
        return response


@dataclass
class DocumentLead:
    url: str
    title: str
    doc_type: str
    date: date | None
    language: str | None
    host: str
    found_on: str
    acquirable: bool
    terms_status: str | None = None
    score: int = 0


@dataclass
class CrawlReport:
    documents: list[DocumentLead] = field(default_factory=list)
    pages_fetched: int = 0
    pages_budget: int = 0
    ir_pages: list[str] = field(default_factory=list)
    robots_skipped: int = 0
    blocked_hosts: list[str] = field(default_factory=list)
    errors: int = 0
    home_status: str = 'ok'          # ok | robots_disallow | unreachable | blocked | not_html
    home_reason: str | None = None
    time_exhausted: bool = False
    filtered_by_date: int = 0
    budget_exhausted: bool = False


def _ir_score(anchor: Anchor, host_family: bool, from_ir_page: bool) -> int:
    if not host_family:
        return 0
    path = unquote(urlsplit(anchor.url).path)
    score = 0
    if _IR_RE.search(anchor.text):
        score += 3
    if _IR_RE.search(path):
        score += 2
    if from_ir_page and (anchor.rel_next or _NEXT_RE.match(anchor.text.strip())):
        score += 2
    return score


def crawl_issuer_site(site_url: str, crawler: IssuerCrawler, *, doc_types: Iterable[str] | None = None,
                      since: date | None = None, until: date | None = None, today: date | None = None) -> CrawlReport:
    """Home page + up to ``crawler.max_pages`` IR pages in total; returns ranked document leads."""
    today = today or datetime.now(timezone.utc).date()
    wanted = set(doc_types or ())
    report = CrawlReport(pages_budget=crawler.max_pages)
    start = _canonical(site_url)
    site_domain = registrable_domain(urlsplit(start).hostname or '')
    queue: list[tuple[int, int, int, str]] = [(0, 0, 0, start)]     # (-score, order, depth, url)
    seen_pages: set[str] = {start}
    seen_docs: dict[str, DocumentLead] = {}
    order = 0
    blocked_hosts: set[str] = set()
    while queue and report.pages_fetched < crawler.max_pages:
        queue.sort()
        _, _, depth, page_url = queue.pop(0)
        is_home = report.pages_fetched == 0 and page_url == start
        try:
            response = crawler.fetch_page(page_url)
        except _Blocked as error:
            if error.code == 'time_budget':
                report.time_exhausted = True
                break
            if error.code == 'robots_disallow':
                report.robots_skipped += 1
            else:
                report.errors += 1
                blocked_hosts.add(urlsplit(page_url).hostname or '')
            if is_home:
                report.home_status = 'robots_disallow' if error.code == 'robots_disallow' else 'blocked'
                report.home_reason = error.code
                break
            continue
        except Exception:
            # Outcome unknown: recorded, never replayed.
            report.errors += 1
            if is_home:
                report.home_status, report.home_reason = 'unreachable', 'issuer_site_unreachable'
                break
            continue
        report.pages_fetched += 1
        content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        if not 200 <= response.status < 300 or (content_type and content_type not in ('text/html', 'application/xhtml+xml')):
            report.errors += 1
            if is_home:
                report.home_status = 'unreachable' if not 200 <= response.status < 300 else 'not_html'
                report.home_reason = 'issuer_site_unreachable' if report.home_status == 'unreachable' else 'issuer_site_not_html'
                break
            continue
        html = response.body.decode(_charset(response.headers.get('content-type', ''), response.body), errors='replace')
        if depth > 0 or _IR_RE.search(urlsplit(page_url).path):
            report.ir_pages.append(page_url)
        from_ir = depth > 0
        for anchor in extract_anchors(html, response.final_url):
            url = _canonical(anchor.url.replace('http://', 'https://', 1) if anchor.url.lower().startswith('http://') else anchor.url)
            parts = urlsplit(url)
            host = parts.hostname or ''
            if parts.scheme != 'https' or not host or len(url) > 500:
                continue
            path_l = unquote(parts.path).lower()
            same_site = registrable_domain(host) == site_domain
            is_pdf = path_l.endswith(_DOC_EXT)
            doc_date = _document_date(anchor, today)
            doc_type = guess_doc_type(anchor.text, anchor.context, unquote(parts.path))
            is_html_doc = (not is_pdf and same_site and doc_date is not None and doc_type != 'other'
                           and path_l.endswith(_HTML_EXT) and not _NEXT_RE.match(anchor.text.strip()))
            if is_pdf or is_html_doc:
                if url in seen_docs:
                    continue
                if (since and doc_date and doc_date < since) or (until and doc_date and doc_date > until):
                    report.filtered_by_date += 1
                    continue
                decision = terms_decision(url)
                acquirable = decision.allowed and (not is_host_blocked(host)) and not is_sec_host(host)
                if not acquirable and host not in blocked_hosts:
                    blocked_hosts.add(host)
                title = _title_for(anchor, url)
                lead = DocumentLead(url, title, doc_type, doc_date, _guess_language(title), host, page_url, acquirable,
                                    None if decision.allowed else decision.status)
                seen_docs[url] = lead
                continue
            if report.pages_fetched + len(queue) >= crawler.max_pages * 3 or depth >= MAX_DEPTH:
                continue
            if url in seen_pages or not same_site or path_l.endswith(('.jpg', '.png', '.gif', '.zip', '.xls', '.xlsx', '.doc', '.docx', '.css', '.js', '.mp4')):
                continue
            score = _ir_score(anchor, same_site, from_ir)
            if score >= 3 or (from_ir and score >= 2):
                seen_pages.add(url)
                order += 1
                queue.append((-score, order, depth + 1, url))
    report.budget_exhausted = bool(queue) and report.pages_fetched >= crawler.max_pages
    report.time_exhausted = report.time_exhausted or crawler.time_exhausted
    report.blocked_hosts = sorted(h for h in blocked_hosts if h)[:10]
    docs = list(seen_docs.values())
    for lead in docs:
        score = 0
        if wanted and lead.doc_type in wanted:
            score += 3
        if lead.doc_type != 'other':
            score += 2
        if lead.date is not None:
            score += 1
            if (today - lead.date).days <= 730:
                score += 1
        if lead.url.lower().endswith('.pdf'):
            score += 1
        if not lead.acquirable:
            score -= 10
        lead.score = score
    docs.sort(key=lambda d: (-d.score, -(d.date.toordinal() if d.date else 0), d.url))
    report.documents = docs
    return report


def _charset(content_type: str, body: bytes) -> str:
    match = re.search(r'charset\s*=\s*["\']?([\w-]+)', content_type, re.I) or re.search(rb'<meta[^>]+charset\s*=\s*["\']?([\w-]+)', body[:4096], re.I)
    name = (match.group(1).decode('ascii', 'ignore') if match and isinstance(match.group(1), bytes) else (match.group(1) if match else 'utf-8'))
    try:
        'x'.encode(name)
        return name
    except LookupError:
        return 'utf-8'


# ---------------------------------------------------------------------------------------------- the tool

@dataclass
class IssuerOutcome:
    result: dict[str, Any]
    leads: list[dict[str, Any]] = field(default_factory=list)
    site_hosts: list[str] = field(default_factory=list)     # candidate website hosts the model may pass as website_url
    summary_title: str | None = None


def _wikidata_market_for_exchange(exchange: str | None) -> Market | None:
    return EXCHANGE_MARKET.get(exchange or '')   # type: ignore[return-value]


class IssuerDocuments:
    """Per-run tool object created by the account-scoped caller; the runtime stays storage and network agnostic."""

    def __init__(self, *, wikidata: WikidataClient | None = None, transport: Callable[..., Any] | None = None,
                 remaining_seconds: Callable[[], float] | None = None, ensure_running: Callable[[], None] = lambda: None,
                 reserve_call: Callable[[], None] = lambda: None, now: Callable[[], datetime] | None = None,
                 sleeper: Callable[[float], None] = time.sleep, delay_seconds: float | None = None,
                 max_pages: int | None = None) -> None:
        self._wikidata = wikidata
        self._transport = transport
        self._remaining = remaining_seconds
        self._ensure_running = ensure_running
        self._reserve_call = reserve_call
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleeper = sleeper
        self._delay = delay_seconds
        self._max_pages = max_pages

    def _failure(self, status: str, reason: str, **extra: Any) -> IssuerOutcome:
        return IssuerOutcome({'status': status, 'reason': reason, **extra})

    def run(self, req: IssuerDocumentsRequest, entities: Mapping[str, Mapping[str, Any]],
            known_hosts: Iterable[str] = ()) -> IssuerOutcome:
        name, ticker, market = req.entity_name, req.ticker, req.market
        if req.entity_id:
            info = entities.get(req.entity_id)
            if not isinstance(info, Mapping):
                return self._failure('failed', 'entity_not_resolved')
            name = name or info.get('company')
            ticker = ticker or info.get('symbol')
            market = market or _wikidata_market_for_exchange(info.get('exchange'))
        if ticker and '.' in ticker:
            head, _, tail = ticker.rpartition('.')
            if head and tail.isalpha() and len(tail) <= 3:
                market = market or {'HK': 'HK', 'SS': 'CN', 'SH': 'CN', 'SZ': 'CN', 'T': 'JP', 'KS': 'KR', 'TW': 'TW'}.get(tail.upper())  # type: ignore[assignment]
                ticker = head
        try:
            self._reserve_call()
        except Exception:
            return self._failure('rate_limited', 'rate_limited')
        deadline_seconds = _env_float(SECONDS_ENV, DEFAULT_SECONDS, 5.0, 600.0)
        if self._remaining is not None:
            deadline_seconds = min(deadline_seconds, self._remaining())
        if deadline_seconds < 5.0:
            return self._failure('failed', 'time_budget')
        started = time.monotonic()
        deadline = started + deadline_seconds
        issuer: dict[str, Any] = {}
        candidates_wire: list[dict[str, Any]] = []
        site: str | None = None
        host_known = {h.lower().rstrip('.') for h in known_hosts if h}
        if req.website_url:
            try:
                site_host = (urlsplit(req.website_url).hostname or '').lower().rstrip('.')
            except ValueError:
                site_host = ''
            if not site_host or urlsplit(req.website_url).scheme != 'https' or not any(
                    site_host == h or site_host.endswith('.' + h) or registrable_domain(site_host) == registrable_domain(h)
                    for h in host_known):
                return self._failure('failed', 'website_not_known', message=(
                    'website_url must be a site already seen in this run (a URL the user wrote, a lead or an earlier '
                    'issuer_documents candidate). Resolve the issuer by name or ticker instead.'))
            site = _canonical(req.website_url)
            issuer = {'website': site, 'website_source': 'known_in_run'}
        else:
            wikidata = self._wikidata or WikidataClient()
            lookup = wikidata.lookup(name=name, ticker=ticker, market=market)
            if lookup.status in ('failed', 'rate_limited'):
                return self._failure('rate_limited' if lookup.status == 'rate_limited' else 'failed',
                                     lookup.reason or 'wikidata_unavailable', message=lookup.message)
            with_site = [p for p in lookup.profiles if p.websites]
            ranked = sorted(with_site, key=lambda p: -p.score(name=name, ticker=ticker, market=market))
            if not ranked:
                return self._failure('empty', 'issuer_website_not_found', attribution=WIKIDATA_ATTRIBUTION,
                                     message=('Wikidata has no official website for this company. Use news_search to find '
                                              "the company's investor-relations page, then pass its URL with website_url "
                                              'only if the user or a lead supplied it.'))
            best = ranked[0]
            top = best.score(name=name, ticker=ticker, market=market)
            runner = ranked[1].score(name=name, ticker=ticker, market=market) if len(ranked) > 1 else -99
            same_site = len(ranked) > 1 and registrable_domain(urlsplit(best.websites[0]).hostname or '') == registrable_domain(
                urlsplit(ranked[1].websites[0]).hostname or '')
            ticker_confirmed = bool(ticker) and top >= 10
            # A name match is only safe when it is clearly better than the next candidate.
            if len(ranked) > 1 and not ticker_confirmed and top - runner < 3 and not same_site:
                candidates_wire = [p.to_wire() for p in ranked[:4]]
                hosts = [urlsplit(w).hostname or '' for p in ranked[:4] for w in p.websites[:1]]
                return IssuerOutcome({'status': 'failed', 'reason': 'issuer_website_ambiguous', 'candidates': candidates_wire,
                                      'attribution': WIKIDATA_ATTRIBUTION,
                                      'message': ('Several companies match. Call again with ticker and market, or with '
                                                  "website_url set to one candidate website.")},
                                     site_hosts=[h for h in hosts if h])
            site = _canonical(best.websites[0])
            issuer = {**best.to_wire(), 'website': site, 'website_source': 'wikidata'}
        crawler = IssuerCrawler(transport=self._transport, deadline=deadline, sleeper=self._sleeper, delay_seconds=self._delay,
                                max_pages=self._max_pages, ensure_running=self._ensure_running)
        report = crawl_issuer_site(site, crawler, doc_types=req.doc_types, since=req.since, until=req.until,
                                   today=self._now().date())
        return self._outcome(req, issuer, site, report)

    def _outcome(self, req: IssuerDocumentsRequest, issuer: dict[str, Any], site: str, report: CrawlReport) -> IssuerOutcome:
        host = urlsplit(site).hostname or ''
        base: dict[str, Any] = {'issuer': issuer}
        if issuer.get('website_source') == 'wikidata':
            base['attribution'] = WIKIDATA_ATTRIBUTION
        coverage = {'pages_fetched': report.pages_fetched, 'pages_budget': report.pages_budget,
                    'ir_pages': [_short(u) for u in report.ir_pages[:8]], 'robots_skipped_pages': report.robots_skipped,
                    'errors': report.errors, 'time_exhausted': report.time_exhausted,
                    'page_budget_exhausted': report.budget_exhausted, 'filtered_by_date': report.filtered_by_date}
        if report.blocked_hosts:
            coverage['not_acquirable_hosts'] = report.blocked_hosts
        if report.home_status == 'robots_disallow':
            return IssuerOutcome({'status': 'denied', 'reason': 'issuer_site_robots_disallow', **base, 'coverage': coverage,
                                  'message': f'{host} robots.txt does not allow automated access; nothing was fetched from it.'},
                                 site_hosts=[host])
        if report.home_status == 'blocked':
            return IssuerOutcome({'status': 'denied', 'reason': 'source_blocked_terms', **base, 'coverage': coverage},
                                 site_hosts=[host])
        if report.home_status in ('unreachable', 'not_html'):
            return IssuerOutcome({'status': 'failed', 'reason': report.home_reason or 'issuer_site_unreachable', **base,
                                  'coverage': coverage, 'external_state': 'unknown'}, site_hosts=[host])
        docs = report.documents
        specific = [d for d in docs if not req.doc_types or d.doc_type in req.doc_types or d.doc_type == 'other']
        shown = specific[:req.limit]
        rows = [{'title': d.title, 'url': d.url, 'date': d.date.isoformat() if d.date else None, 'doc_type': d.doc_type,
                 'language': d.language, 'host': d.host, 'found_on': _short(d.found_on),
                 **({} if d.acquirable else {'acquirable': False, 'terms_status': d.terms_status or 'blocked'})}
                for d in shown]
        leads = [{'url': d.url, 'title': d.title,
                  'published_at': f'{d.date.isoformat()}T00:00:00+00:00' if d.date else None,
                  'domain': d.host, 'source_type': 'issuer_website', 'doc_type': d.doc_type,
                  'language': d.language, 'discovered_by': 'issuer_documents', 'found_on': d.found_on,
                  **({} if d.acquirable else {'acquirable': False, 'terms_status': d.terms_status or 'blocked'})}
                 for d in shown]
        leads = [{k: v for k, v in lead.items() if v is not None} for lead in leads]
        result: dict[str, Any] = {'status': 'ok', 'reason': None, **base, 'documents': rows, 'coverage': coverage,
                                  'warning': 'Document links are unverified leads, not evidence; pass url values to acquire_sources and read them before citing.'}
        if not rows:
            result.update(status='empty', reason='issuer_no_documents_found',
                          message=('No document links were found within the crawl budget. The site may list documents '
                                   'through scripts. Try news_search for the company, or ask the user to upload the document.'))
        elif report.time_exhausted or report.budget_exhausted or report.errors:
            result['status'] = 'partial'
            result['message'] = 'The crawl hit its page or time budget or some pages failed; the list may be incomplete.'
        if req.doc_types and any(d.doc_type == 'other' for d in shown):
            result['note'] = "Rows with doc_type 'other' could not be classified; check their titles."
        if report.blocked_hosts:
            result['blocked_documents_note'] = ('Some links point at hosts whose terms do not permit automated access (for '
                                                'example HKEXnews or cninfo); they are listed as not acquirable.')
        return IssuerOutcome(result, leads=leads, site_hosts=[host],
                             summary_title=f'{len(rows)} 条文档线索 · {host}')


def _short(url: str) -> str:
    parts = urlsplit(url)
    return (parts.netloc + parts.path + (('?' + parts.query) if parts.query else ''))[:160]


def call_signature(req: IssuerDocumentsRequest) -> str:
    payload = req.model_dump(mode='json', exclude={'rationale'})
    return hashlib.sha256(repr(sorted(payload.items())).encode()).hexdigest()


def known_hosts_from_text(text: str) -> list[str]:
    hosts: list[str] = []
    for match in re.finditer(r'https?://[A-Za-z0-9\-._~:/?#\[\]@!$&*+,;=%()]+', text or ''):
        try:
            host = (urlsplit(match.group(0)).hostname or '').lower().rstrip('.')
        except ValueError:
            continue
        if host and host not in hosts:
            hosts.append(host)
    return hosts
