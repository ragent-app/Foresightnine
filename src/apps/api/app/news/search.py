"""``news_search``: merge GLM MCP web search and GDELT into annotated, clustered news leads (plan W5).

Providers implement ``name``, ``supports_time_filter`` and ``search(query, *, since, until, languages, limit)``
returning a ``ToolResult`` whose ``data`` is a list of ``NewsLead``. Wrappers adapt the existing GLM and GDELT
clients. Leads are metadata only: ``acquirable`` says whether site terms permit automated retrieval of the body.
"""
from __future__ import annotations

import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any, Literal, Protocol, Sequence

from ..fin_data.models import Coverage, Cost, Permissions, Provenance, SuggestedAction, ToolResult
from ..research_discovery import RedirectLead, unwrap_redirect_url
from ..research_source_terms import terms_decision
from .clustering import (
    _AGENCIES,
    _AGGREGATORS,
    MIN_FULL_BODY_CHARS,
    ClusterInput,
    canonical_news_url,
    cluster_items,
    registrable_domain,
)
from .gdelt import GDELT_ATTRIBUTION, GdeltClient, NewsLead

_CJK = re.compile(r"[㐀-鿿]")
_CJK_RUN = re.compile(r"[㐀-鿿]+")
_ASCII_ALNUM = re.compile(r"[A-Za-z0-9_]")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")
_OFFICIAL_SUFFIXES = (".gov", ".gov.cn", ".gov.uk", ".gov.hk", ".go.jp", ".go.kr", ".europa.eu", ".int", ".mil")
_OFFICIAL_HOSTS = ("sec.gov", "federalreserve.gov", "ecb.europa.eu", "boj.or.jp", "pbc.gov.cn", "stats.gov.cn", "hkexnews.hk",
                   "bankofengland.co.uk", "imf.org", "worldbank.org", "oecd.org", "bis.org")
_WIRE_HOSTS = frozenset(h for hosts, _ in _AGENCIES.values() for h in hosts)
_MAJOR_FINANCIAL_PRESS = frozenset({
    "wsj.com", "ft.com", "cnbc.com", "marketwatch.com", "barrons.com", "economist.com",
    "nikkei.com", "scmp.com", "caixin.com", "yicai.com", "stcn.com", "cls.cn", "21jingji.com",
    "kyodonews.net", "yonhapnews.co.kr", "yna.co.kr", "bbc.com", "bbc.co.uk", "nytimes.com",
    "washingtonpost.com", "theguardian.com", "forbes.com", "fortune.com", "prnewswire.com",
    "businesswire.com", "globenewswire.com",
})
_EXTRA_AGGREGATORS = frozenset({"news.google.com", "apple.news", "feedproxy.google.com"})
AUTHORITY_TIER_WEIGHTS: dict[str, float] = {
    "official": 1.5,
    "wire_agency": 1.25,
    "general": 1.0,
    "aggregator": 0.7,
}
_RECENCY_HALF_LIFE_DAYS = 14.0
_UNDATED_RECENCY_DECAY = 0.20
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "is", "are",
    "was", "were", "with", "by", "from", "as", "about", "into", "over",
})
_CASE_SENSITIVE_SHORT_TICKERS = frozenset({
    "a", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it",
    "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "all", "and",
    "are", "but", "can", "day", "for", "has", "new", "not", "now", "one", "out",
    "see", "the", "two", "way", "who", "you",
})

EVENT_TYPES: tuple[str, ...] = (
    "earnings",
    "guidance",
    "m_and_a",
    "regulatory",
    "macro",
    "rating_change",
    "corporate_action",
)
_WB_L = r"(?<![A-Za-z0-9_])"
_WB_R = r"(?![A-Za-z0-9_])"
_EVENT_PATTERNS: dict[str, re.Pattern[str]] = {
    "earnings": re.compile(
        rf"{_WB_L}(?:earnings|quarterly results?|results? for the quarter|q[1-4]\s+(?:results?|earnings|revenue|profit|loss)|"
        rf"fy\s*\d{{2,4}}\s+(?:results?|earnings)|annual report|10-k|10-q|eps|net income|net profit|net loss|"
        rf"revenue beat|earnings beat|revenue miss|earnings miss|beats? estimates?|misses? estimates?|profit warning|"
        rf"preliminary results?){_WB_R}|"
        r"财报|业绩(?:快报|预告|报告|公布|超预期|不及预期|承压)|季报|中报|半年报|年报|营收|营业收入|净利润|净亏损|扣非|每股收益|盈利预喜|盈利预警|盈喜|盈警",
        re.I,
    ),
    "guidance": re.compile(
        rf"{_WB_L}(?:guidance|outlook|forecast|full[- ]year\s+(?:outlook|target|guidance)|"
        rf"raises?\s+(?:guidance|outlook|forecast)|lowers?\s+(?:guidance|outlook|forecast)|"
        rf"cuts?\s+(?:guidance|outlook|forecast)|reaffirms?\s+(?:guidance|outlook)|withdraws?\s+guidance|"
        rf"expects?\s+(?:q[1-4]|full[- ]year|fy\d{{2,4}})){_WB_R}|"
        r"业绩指引|盈利指引|指引|前景展望|业绩展望|上调(?:预期|指引|目标)|下调(?:预期|指引|目标)|调高(?:预期|指引)|调低(?:预期|指引)|维持(?:指引|预期)|预期(?:增长|下滑)",
        re.I,
    ),
    "m_and_a": re.compile(
        rf"{_WB_L}(?:mergers?|acquires?|acquiring|acquisition|takeover|buyout|lbo|m&a|joint venture|"
        rf"spin[- ]?off|divestiture|divests?|tender offer|all[- ]cash deal|agrees to buy|plans to buy){_WB_R}|"
        r"并购|收购|兼并|合并|要约收购|借壳|重组|重大资产重组|分拆|剥离|出售资产|合资(?:公司|企业)|入股|控股权",
        re.I,
    ),
    "regulatory": re.compile(
        rf"{_WB_L}(?:sec|ftc|doj|fda|cftc|finra|fcc|csrc|samr|antitrust|anti[- ]trust|monopoly|"
        rf"regulator|regulatory|enforcement|investigation|probe|subpoena|lawsuit|settlement|"
        rf"fine|fined|penalty|penalties|consent decree|compliance|"
        rf"(?:regulatory|sec|ftc|fda|antitrust|government)\s+approvals?|"
        rf"approved\s+by\s+(?:regulators?|the\s+(?:sec|ftc|fda|doj|csrc|samr))|"
        rf"antitrust\s+clearance|regulatory\s+clearance|sanctions?|tariffs?|export controls?){_WB_R}|"
        r"监管|证监会|银保监|金管局|反垄断|反不正当竞争|立案调查|行政处罚|罚款|罚单|合规|审批|获批|核准|监管批准|获监管批准|问询函|警示函|监管函|约谈|制裁|关税|出口管制",
        re.I,
    ),
    "macro": re.compile(
        rf"{_WB_L}(?:fed|federal reserve|fomc|ecb|boe|boj|pboc|central bank|interest rates?|rate cut|rate hike|"
        rf"rate decision|holds? rates?|policy rate|benchmark rate|monetary policy|fiscal policy|inflation|"
        rf"cpi|pce|ppi|gdp|pmi|payrolls|unemployment|treasury yields?|bond yields?|quantitative tightening|quantitative easing){_WB_R}|"
        r"央行|美联储|欧洲央行|英国央行|日本央行|人民银行|利率|降息|加息|基准利率|货币政策|财政政策|通胀|通货膨胀|消费者物价|非农|失业率|国债收益率|降准|mlf|lpr|宏观",
        re.I,
    ),
    "rating_change": re.compile(
        rf"{_WB_L}(?:upgrades?|upgraded|downgrades?|downgraded|initiates? coverage|resumes? coverage|"
        rf"reiterates?\s+(?:buy|sell|hold|overweight|underweight|neutral|outperform|underperform)|"
        rf"price target|target price|raises?\s+target|cuts?\s+target|credit rating|moody'?s|s&p|fitch){_WB_R}|"
        r"评级|上调评级|下调评级|调高评级|调低评级|维持买入|买入评级|增持评级|减持评级|卖出评级|中性评级|跑赢大市|目标价|首次覆盖|信用评级|穆迪|标普|惠誉",
        re.I,
    ),
    "corporate_action": re.compile(
        rf"{_WB_L}(?:special dividend|dividend|share repurchase|buyback|stock split|reverse split|"
        rf"rights issue|rights offering|ipo|initial public offering|secondary offering|follow[- ]on offering|"
        rf"delisting|delist|bankruptcy|chapter 11|restructuring|ceo\s+(?:resigns?|steps? down|appointed|named)|"
        rf"board of directors|shareholder vote|proxy){_WB_R}|"
        r"分红|派息|股息|特别股息|回购|股份回购|拆股|合股|配股|增发|定增|首次公开发行|上市|退市|停牌|复牌|破产|重整|更换ceo|高管变动|董事会|股东大会",
        re.I,
    ),
}


QUOTA_REASONS = frozenset({"search_quota_exhausted"})
RATE_REASONS = frozenset({"search_rate_limited", "search_provider_rate_limited", "gdelt_rate_limited"})
LIMIT_REASONS = QUOTA_REASONS | RATE_REASONS
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 45.0


def limited_failure_result(error: Any, prov: Provenance, cost: Cost) -> ToolResult:
    """Convert a search ``ProviderFailure`` into a ``ToolResult`` keeping its code, message and reset time."""
    code = str(getattr(error, "code", "search_provider_unavailable"))
    retryable = bool(getattr(error, "retryable", True))
    message = str(error)[:300] if hasattr(error, "code") else "Search provider failed."
    reset_at = getattr(error, "reset_at", None)
    if code in QUOTA_REASONS and isinstance(reset_at, str) and reset_at:
        message = f"{message} (quota resets at {reset_at})"[:400]
    status = "rate_limited" if code in RATE_REASONS else "failed"
    if code in QUOTA_REASONS:
        retryable = False
    return ToolResult.failure(status, code, message, retryable=retryable or status == "rate_limited", provenance=prov, cost=cost)


class NewsProvider(Protocol):
    name: str
    supports_time_filter: bool

    def search(self, query: str, *, since: datetime | date | None, until: datetime | date | None,
               languages: Sequence[str] | None, limit: int) -> ToolResult: ...


def classify_source_type(domain: str | None, default: str) -> str:
    host = (domain or "").lower()
    if host and (host.endswith(_OFFICIAL_SUFFIXES) or any(host == h or host.endswith("." + h) for h in _OFFICIAL_HOSTS)):
        return "official"
    return default


def guess_language(text: str) -> str | None:
    """Only script detection (CJK); returns None rather than guessing among Latin-script languages."""
    letters = [c for c in text if c.isalpha()]
    if letters and len(_CJK.findall(text)) / len(letters) >= 0.3:
        return "zh"
    return None


class GdeltNewsProvider:
    name = "gdelt"
    supports_time_filter = True   # on GDELT's first-seen time, not publication time

    def __init__(self, client: GdeltClient | None = None) -> None:
        self.client = client or GdeltClient()

    def search(self, query, *, since=None, until=None, languages=None, limit=50) -> ToolResult:
        return self.client.search(query, since=since, until=until, languages=languages, limit=limit)


class GlmNewsProvider:
    """Adapter over ``GlmCodingMcpSearchEvidenceProvider.search`` (no provider-side time filter)."""
    name = "glm-coding-mcp-search"
    supports_time_filter = False

    def __init__(self, provider: Any = None) -> None:
        if provider is None:
            from ..glm_mcp_search import GlmCodingMcpSearchEvidenceProvider
            provider = GlmCodingMcpSearchEvidenceProvider.from_environment()
        self.provider = provider

    def search(self, query, *, since=None, until=None, languages=None, limit=10) -> ToolResult:
        now = datetime.now(timezone.utc)
        prov = Provenance(provider=self.name, endpoint="glm-coding-mcp", retrieved_at=now)
        if getattr(self.provider, "supports_project_search", True) is False:
            return ToolResult.failure("denied", "search_provider_unconfigured", "GLM web search is not configured.", provenance=prov)
        try:
            candidates = self.provider.search(query, limit=max(1, min(int(limit), 10)))
        except Exception as error:   # ProviderFailure carries safe code/retryable; anything else is opaque
            return limited_failure_result(error, prov, Cost(provider_calls=1))
        leads: list[NewsLead] = []
        for c in candidates or ():
            published = getattr(c, "published_at", None)
            if isinstance(published, datetime):
                published_dt, precision = published, "datetime"
            elif isinstance(published, date):
                published_dt, precision = datetime(published.year, published.month, published.day, tzinfo=timezone.utc), "date"
            else:
                published_dt, precision = None, None
            title = getattr(c, "title", "") or ""
            leads.append(NewsLead(url=c.url, title=title, excerpt=getattr(c, "excerpt", None), publisher=getattr(c, "publisher", None),
                                  language=guess_language(title), published_at=published_dt, published_precision=precision,
                                  providers=[self.name]))
        if not leads:
            return ToolResult(status="empty", reason="no_results", message="Search returned no candidates.", provenance=prov, cost=Cost(provider_calls=1))
        return ToolResult(status="ok", reason="ok", coverage=Coverage(rows=len(leads)), provenance=prov, cost=Cost(provider_calls=1),
                          permissions=Permissions(store=True, display=True, model_input=True, export=False), data=leads)


def _utc(value: datetime | date | None, end: bool = False) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if end:
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=timezone.utc)
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)


def _host_from_url(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    return m.group(1).lower() if m else ""


@lru_cache(maxsize=512)
def _compile_entity_pattern(normalized_entity: str) -> re.Pattern[str] | None:
    parts = normalized_entity.split()
    if not parts:
        return None
    left = _WB_L if _ASCII_ALNUM.match(normalized_entity[0]) else ""
    right = _WB_R if _ASCII_ALNUM.match(normalized_entity[-1]) else ""
    body = r"\s+".join(re.escape(p) for p in parts)
    case_sensitive = (
        normalized_entity.isascii()
        and normalized_entity.isupper()
        and normalized_entity.lower() in _CASE_SENSITIVE_SHORT_TICKERS
    )
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(left + body + right, flags)


def entity_matches(entity: str, text: str) -> bool:
    """Match an entity/ticker with ASCII word-boundary and CJK-aware matching."""
    norm_entity = unicodedata.normalize("NFKC", entity or "").strip()
    if not norm_entity or not text:
        return False
    pattern = _compile_entity_pattern(norm_entity)
    if pattern is None:
        return False
    return pattern.search(unicodedata.normalize("NFKC", text)) is not None


def match_entities(entities: Sequence[str], text: str) -> list[str]:
    """Return entities from ``entities`` that match ``text`` cleanly."""
    if not entities or not text:
        return []
    return [e for e in entities if isinstance(e, str) and e.strip() and entity_matches(e, text)]


def tag_news_events(title: str, excerpt: str | None = None) -> list[str]:
    """Deterministically tag bilingual (EN/ZH) financial event types in canonical order."""
    combined = unicodedata.normalize("NFKC", f"{title or ''} {excerpt or ''}").strip()
    if not combined:
        return []
    return [event_type for event_type in EVENT_TYPES if _EVENT_PATTERNS[event_type].search(combined)]


def classify_authority_tier(lead: NewsLead) -> str:
    """Classify a lead's source authority tier: official > wire_agency > general > aggregator."""
    host = (lead.domain or _host_from_url(lead.url)).lower().lstrip(".")
    if lead.source_type == "official" or classify_source_type(host, "") == "official":
        return "official"
    reg = registrable_domain(host) if host else ""
    if host in _EXTRA_AGGREGATORS or reg in _AGGREGATORS:
        return "aggregator"
    if host in _WIRE_HOSTS or reg in _WIRE_HOSTS or host in _MAJOR_FINANCIAL_PRESS or reg in _MAJOR_FINANCIAL_PRESS:
        return "wire_agency"
    return "general"


def default_providers() -> list[NewsProvider]:
    providers: list[NewsProvider] = []
    try:
        glm = GlmNewsProvider()
        if getattr(glm.provider, "supports_project_search", False):
            providers.append(glm)
    except Exception:
        pass
    providers.append(GdeltNewsProvider())
    return providers


def _merge(leads: list[NewsLead]) -> list[NewsLead]:
    merged: dict[str, NewsLead] = {}
    for lead in leads:
        key = canonical_news_url(lead.url)
        current = merged.get(key)
        if current is None:
            merged[key] = lead.model_copy(update={"canonical_url": key})
            continue
        update: dict[str, Any] = {"providers": sorted(set(current.providers) | set(lead.providers)),
                                  "attribution": sorted(set(current.attribution) | set(lead.attribution))}
        for field in ("published_at", "published_precision", "seen_at", "excerpt", "publisher", "domain", "language", "source_country"):
            if getattr(current, field) is None and getattr(lead, field) is not None:
                update[field] = getattr(lead, field)
        if current.source_type == "unknown" and lead.source_type != "unknown":
            update["source_type"] = lead.source_type
        merged[key] = current.model_copy(update=update)
    return list(merged.values())


def _annotate(lead: NewsLead, entities: Sequence[str]) -> NewsLead:
    decision = terms_decision(lead.url)
    redirect = False
    try:
        unwrap_redirect_url(lead.url)
    except RedirectLead:
        redirect = True
    except Exception:
        pass
    host = lead.domain
    if not host:
        host = _host_from_url(lead.url) or None
    haystack = f"{lead.title} {lead.excerpt or ''}"
    return lead.model_copy(update={
        "domain": host, "terms_status": decision.status, "terms_reason": decision.reason_code,
        "acquirable": decision.allowed and not redirect, "unresolved_redirect": redirect,
        "source_type": classify_source_type(host, lead.source_type),
        "matched_entities": match_entities(entities, haystack)})


def _in_window(lead: NewsLead, since: datetime | None, until: datetime | None) -> bool | None:
    """True/False for leads with a publication time; None when undated."""
    if lead.published_at is None:
        return None
    moment = _utc(lead.published_at)
    if lead.published_precision == "date":
        day = moment.date()
        return (since is None or day >= since.date()) and (until is None or day <= until.date())
    return (since is None or moment >= since) and (until is None or moment <= until)


def _lead_sort_key(lead: NewsLead) -> float:
    moment = _utc(lead.published_at) or _utc(lead.seen_at)
    return -moment.timestamp() if moment is not None else float("inf")


def _extract_query_terms(query: str) -> tuple[list[str], list[str]]:
    norm = unicodedata.normalize("NFKC", query or "")
    raw_ascii = [t.lower() for t in _ASCII_WORD.findall(norm)]
    cjk_runs = list(dict.fromkeys(_CJK_RUN.findall(norm)))
    filtered_ascii = [t for t in raw_ascii if t not in _STOPWORDS]
    ascii_terms = list(dict.fromkeys(filtered_ascii if (filtered_ascii or cjk_runs) else raw_ascii))
    return ascii_terms, cjk_runs


def _text_term_coverage(ascii_terms: Sequence[str], cjk_runs: Sequence[str], text: str) -> float:
    total = len(ascii_terms) + len(cjk_runs)
    if total == 0:
        return 1.0
    norm_text = unicodedata.normalize("NFKC", text or "")
    if not norm_text:
        return 0.0
    hits = 0.0
    for term in ascii_terms:
        if re.search(rf"{_WB_L}{re.escape(term)}{_WB_R}", norm_text, re.I):
            hits += 1.0
    for run in cjk_runs:
        if run in norm_text:
            hits += 1.0
        elif len(run) >= 2:
            bigrams = [run[i:i + 2] for i in range(len(run) - 1)]
            hits += sum(1 for bg in bigrams if bg in norm_text) / len(bigrams)
    return hits / total


def _cluster_relevance(query: str, entities: Sequence[str], members: Sequence[NewsLead]) -> float:
    ascii_terms, cjk_runs = _extract_query_terms(query)
    best_lexical = 0.25
    for member in members:
        title_cov = _text_term_coverage(ascii_terms, cjk_runs, member.title)
        excerpt_cov = _text_term_coverage(ascii_terms, cjk_runs, member.excerpt or "") if member.excerpt else 0.0
        lexical = 0.25 + 0.55 * title_cov + 0.20 * excerpt_cov
        if lexical > best_lexical:
            best_lexical = lexical
    unique_entities = {e.strip().casefold() for e in entities if isinstance(e, str) and e.strip()}
    if not unique_entities:
        return best_lexical
    matched = {e.strip().casefold() for member in members for e in member.matched_entities}
    entity_bonus = 0.35 * (len(matched) / len(unique_entities))
    return best_lexical + entity_bonus


def _recency_decay(lead: NewsLead, *, ref_time: datetime) -> float:
    moment = _utc(lead.published_at) or _utc(lead.seen_at)
    if moment is None:
        return _UNDATED_RECENCY_DECAY
    age_days = max(0.0, (ref_time - moment).total_seconds() / 86400.0)
    return 1.0 / (1.0 + age_days / _RECENCY_HALF_LIFE_DAYS)


def news_search(query: str, *, since: datetime | date | None = None, until: datetime | date | None = None,
                languages: Sequence[str] | None = None, entities: Sequence[str] | None = None,
                source_types: Sequence[str] | None = None, limit: int = 20,
                providers: Sequence[NewsProvider] | None = None,
                timeout_seconds: float | None = None,
                sort_by: Literal["relevance", "recency"] = "relevance",
                as_of: datetime | date | None = None) -> ToolResult:
    """Search news across providers; returns clusters (``data`` items are ``NewsGroup`` dicts) with coverage notes."""
    now = datetime.now(timezone.utc)
    if not isinstance(query, str) or not query.strip():
        return ToolResult.failure("failed", "news_query_invalid", "Query is empty.")
    if sort_by not in ("relevance", "recency"):
        return ToolResult.failure("failed", "news_sort_invalid", "sort_by must be 'relevance' or 'recency'.")
    limit = max(1, min(int(limit), 100))
    entity_list = [e.strip() for e in entities or () if isinstance(e, str) and e.strip()]
    full_query = query.strip()
    extra = [e for e in entity_list if not entity_matches(e, full_query)]
    if extra:
        full_query += " " + " ".join(f'"{e}"' if " " in e else e for e in extra)
    chosen = list(providers) if providers is not None else default_providers()
    if not chosen:
        return ToolResult.failure("failed", "no_news_provider", "No news provider is configured.")
    since_utc, until_utc = _utc(since), _utc(until, end=True)
    ref_time = _utc(as_of, end=True) or now

    answered: list[str] = []
    failed: list[tuple[str, str, bool, str, str]] = []
    leads: list[NewsLead] = []
    calls = 0
    provider_filtered: list[str] = []
    effective_timeout = (DEFAULT_PROVIDER_TIMEOUT_SECONDS if timeout_seconds is None
                         else max(0.01, float(timeout_seconds)))
    provider_limit = min(250, limit * 2)
    pool = ThreadPoolExecutor(max_workers=len(chosen), thread_name_prefix="news-search")
    timed_out = False
    futures = []
    done = set()
    try:
        futures = [
            pool.submit(
                provider.search,
                full_query,
                since=since,
                until=until,
                languages=languages,
                limit=provider_limit,
            )
            for provider in chosen
        ]
        done, not_done = wait(futures, timeout=effective_timeout)
        if not_done:
            timed_out = True
            for fut in not_done:
                fut.cancel()
    finally:
        pool.shutdown(wait=not timed_out, cancel_futures=timed_out)

    for provider, future in zip(chosen, futures):
        if future not in done:
            failed.append((provider.name, "provider_timeout", True, "failed", ""))
            continue
        try:
            result = future.result()
        except TimeoutError:
            failed.append((provider.name, "provider_timeout", True, "failed", ""))
            continue
        except Exception:
            failed.append((provider.name, "provider_exception", True, "failed", ""))
            continue
        calls += result.cost.provider_calls or 1
        if result.status in ("ok", "partial", "empty"):
            answered.append(provider.name)
            if provider.supports_time_filter and (since is not None or until is not None):
                provider_filtered.append(provider.name)
            for lead in result.data:
                if isinstance(lead, NewsLead):
                    leads.append(lead.model_copy(update={"time_filter": "provider_seen_at" if provider.supports_time_filter and (since or until) and lead.published_at is None else lead.time_filter}))
        else:
            failed.append((provider.name, result.reason, result.retryable, result.status, result.message))

    notes: list[str] = []
    notes.append("Providers answered: " + (", ".join(answered) or "none") + ".")
    for name, reason, retryable, status, detail in failed:
        notes.append(f"Provider {name} {status} ({reason}{'; retryable' if retryable else ''})"
                     + (f": {detail[:300]}" if detail and reason in LIMIT_REASONS else "") + ".")
    gdelt_used = any(p.name == "gdelt" for p in chosen)
    if gdelt_used:
        notes.append(GDELT_ATTRIBUTION)

    prov = Provenance(provider="news_search", endpoint="news_search", retrieved_at=now, endpoints=[p.name for p in chosen])
    cost = Cost(provider_calls=calls)
    requested = {"start": since_utc.isoformat() if since_utc else None, "end": until_utc.isoformat() if until_utc else None}
    if not answered:
        reasons = {reason for _, reason, *_ in failed}
        if failed and reasons <= LIMIT_REASONS:
            # Every provider is quota/rate limited: say so (with each provider's reason, message and reset time).
            reason = "search_quota_exhausted" if reasons & QUOTA_REASONS else "search_rate_limited"
            retryable = any(r for _, _, r, _, _ in failed)
            status = "rate_limited" if retryable else "failed"
            detail = "; ".join(f"{name}: {why}" + (f" - {msg[:300]}" if msg else "") for name, why, _, _, msg in failed)
            return ToolResult.failure(status, reason, "Search is limited: " + detail, retryable=retryable,
                                      suggested_actions=[SuggestedAction(action="retry", retry_after_seconds=5.0)] if retryable else [],
                                      provenance=prov, cost=cost, coverage=Coverage(requested=requested, notes=notes))
        statuses = {s for _, _, _, s, _ in failed}
        status = "rate_limited" if statuses == {"rate_limited"} else "failed"
        return ToolResult.failure(status, "all_providers_failed", " ".join(notes), retryable=any(r for _, _, r, _, _ in failed),
                                  suggested_actions=[SuggestedAction(action="retry", retry_after_seconds=5.0)] if status == "rate_limited" else [],
                                  provenance=prov, cost=cost, coverage=Coverage(requested=requested, notes=notes))

    merged = _merge(leads)
    annotated = [_annotate(l, entity_list) for l in merged]

    kept: list[NewsLead] = []
    dropped = undated = 0
    type_dropped = lang_dropped = 0
    wanted_types = {t.lower() for t in source_types or ()}
    wanted_langs = {l.lower()[:2] for l in languages or ()}
    for lead in annotated:
        verdict = _in_window(lead, since_utc, until_utc) if (since_utc or until_utc) else True
        if verdict is None:
            undated += 1
        elif verdict is False:
            dropped += 1
            continue
        elif since_utc or until_utc:
            lead = lead.model_copy(update={"time_filter": "local_published_at"})
        if wanted_types and lead.source_type != "unknown" and lead.source_type not in wanted_types:
            type_dropped += 1
            continue
        if wanted_langs and lead.language and lead.language.lower()[:2] not in wanted_langs:
            lang_dropped += 1
            continue
        kept.append(lead)

    if since_utc or until_utc:
        notes.append(f"Time window: provider-side on first-seen time for {', '.join(provider_filtered) or 'no provider'}; "
                     f"local filter on provider-stated publication dates for the rest. Kept {len(kept)}, dropped {dropped} "
                     f"outside the window, {undated} have no known publication date (kept; published time unknown, not guessed).")
        local_only = [n for n in answered if n not in provider_filtered]
        if local_only:
            notes.append(f"Provider(s) {', '.join(local_only)} do not support time filtering; results were filtered locally.")
    if type_dropped:
        notes.append(f"{type_dropped} leads dropped by source_types filter.")
    if lang_dropped:
        notes.append(f"{lang_dropped} leads dropped by language filter (leads of unknown language are kept).")

    result = cluster_items([ClusterInput(id=str(i), url=l.url, title=l.title,
                                         text=l.excerpt if l.excerpt and len(l.excerpt) >= MIN_FULL_BODY_CHARS else None,
                                         published_at=l.published_at, seen_at=l.seen_at, domain=l.domain) for i, l in enumerate(kept)])
    notes.extend(result.notes)
    groups: list[dict[str, Any]] = []
    raw_scores: dict[int, float] = {}
    for cluster in result.clusters:
        members = [kept[int(i)].model_copy(update={"cluster_id": cluster.cluster_id}) for i in cluster.member_ids]
        rep = members[0]
        event_set = {tag for member in members for tag in tag_news_events(member.title, member.excerpt)}
        cluster_events = [et for et in EVENT_TYPES if et in event_set]
        tier = max((classify_authority_tier(m) for m in members), key=lambda t: AUTHORITY_TIER_WEIGHTS[t])
        moment_lead = rep if (_utc(rep.published_at) or _utc(rep.seen_at)) else next(
            (m for m in members if _utc(m.published_at) or _utc(m.seen_at)),
            rep,
        )
        score = _cluster_relevance(query, entity_list, members) * _recency_decay(moment_lead, ref_time=ref_time) * AUTHORITY_TIER_WEIGHTS[tier]
        group = {
            "cluster_id": cluster.cluster_id,
            "independent_source_count": cluster.independent_source_count,
            "independent_sources": list(cluster.independent_sources),
            "syndicated_from": list(cluster.syndicated_from),
            "representative": rep,
            "members": members,
            "event_types": cluster_events,
            "authority_tier": tier,
            "score": round(score, 6),
        }
        raw_scores[id(group)] = score
        groups.append(group)
    if sort_by == "recency":
        groups.sort(key=lambda g: (
            _lead_sort_key(g["representative"]),
            g["representative"].canonical_url or canonical_news_url(g["representative"].url),
        ))
    else:
        groups.sort(key=lambda g: (
            -raw_scores[id(g)],
            _lead_sort_key(g["representative"]),
            g["representative"].canonical_url or canonical_news_url(g["representative"].url),
        ))
    truncated = len(groups) > limit
    groups = groups[:limit]
    notes.append(f"{sum(len(g['members']) for g in groups)} leads in {len(groups)} clusters.")
    attribution = [GDELT_ATTRIBUTION] if gdelt_used else []
    if not groups:
        return ToolResult(status="empty", reason="no_news_found", message=" ".join(notes), provenance=prov, cost=cost,
                          coverage=Coverage(requested=requested, rows=0, notes=notes))
    partial = bool(failed)
    return ToolResult(status="partial" if partial else "ok", reason="provider_failed" if partial else "ok",
                      message=" ".join(attribution), retryable=False, coverage=Coverage(requested=requested, rows=len(groups), truncated=truncated, notes=notes),
                      permissions=Permissions(store=True, display=True, model_input=True, export=False), provenance=prov, cost=cost, data=groups)
