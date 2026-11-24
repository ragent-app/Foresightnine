"""Deterministic default-entry auto-routing for single-company US earnings research.

Routes single-company US equity earnings review questions (Chinese and English)
to the Scheme 2 Buy-Side Earnings Squad while keeping multi-company comparisons,
macro/central-bank queries, IPO-day queries, non-US markets, and explicit
opt-outs / private-only runs on the single-coordinator research path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from .fin_data import cn as cn_helpers
from .fin_data.adapters.sec_edgar import SecEdgarAdapter, cik10
from .fin_data.entity import EntityCandidate, MARKETS, parse_entity_id, resolve_entity
from .fin_data.models import EntityRef, ToolResult

RunMode = Literal["earnings_squad", "legacy_single_loop"]

US_EXCHANGES: frozenset[str] = MARKETS["US"] | frozenset({"AMEX", "NYSE ARCA", "BATS", "ARCA"})
NON_US_EXCHANGES: frozenset[str] = frozenset(
    {
        ex
        for market_key, ex_set in MARKETS.items()
        if market_key != "US"
        for ex in ex_set
    }
    | {"SEHK", "HKG", "SHG", "SHE", "EURONEXT", "XSHG", "XSHE", "XHKG"}
)

_OPT_OUT_SOURCES_MODES: frozenset[str] = frozenset({
    "empty",
    "private_only",
    "private_upload_only",
    "local_only",
    "disabled",
    "off",
    "none",
})
_ALLOWED_SOURCES_MODES: frozenset[str] = frozenset({"", "auto", "selected", "sec", "default"})


@dataclass(frozen=True)
class VerifiedUsIssuer:
    symbol: str
    cik: str
    company_name: str
    sic_code: str | None = None


# Verified US SEC issuers (cross-checked against direction_a_golden_eval_set.json / SEC EDGAR).
_VERIFIED_US_ISSUERS: dict[str, VerifiedUsIssuer] = {
    "AAPL": VerifiedUsIssuer("AAPL", "0000320193", "Apple Inc.", "3571"),
    "AMD": VerifiedUsIssuer("AMD", "0000002488", "ADVANCED MICRO DEVICES INC", "3674"),
    "AMZN": VerifiedUsIssuer("AMZN", "0001018724", "AMAZON COM INC", "5961"),
    "AVGO": VerifiedUsIssuer("AVGO", "0001730168", "Broadcom Inc.", "3674"),
    "COST": VerifiedUsIssuer("COST", "0000909832", "COSTCO WHOLESALE CORP /NEW", "5331"),
    "CRM": VerifiedUsIssuer("CRM", "0001108524", "Salesforce, Inc.", "7372"),
    "GOOGL": VerifiedUsIssuer("GOOGL", "0001652044", "Alphabet Inc.", "7370"),
    "GOOG": VerifiedUsIssuer("GOOG", "0001652044", "Alphabet Inc.", "7370"),
    "JNJ": VerifiedUsIssuer("JNJ", "0000200406", "JOHNSON & JOHNSON", "2834"),
    "JPM": VerifiedUsIssuer("JPM", "0000019617", "JPMORGAN CHASE & CO", "6021"),
    "LLY": VerifiedUsIssuer("LLY", "0000059478", "ELI LILLY & Co", "2834"),
    "META": VerifiedUsIssuer("META", "0001326801", "Meta Platforms, Inc.", "7370"),
    "MRK": VerifiedUsIssuer("MRK", "0000310158", "Merck & Co., Inc.", "2834"),
    "MSFT": VerifiedUsIssuer("MSFT", "0000789019", "MICROSOFT CORP", "7372"),
    "NFLX": VerifiedUsIssuer("NFLX", "0001065280", "NETFLIX INC", "7841"),
    "NKE": VerifiedUsIssuer("NKE", "0000320187", "NIKE, Inc.", "3021"),
    "NOW": VerifiedUsIssuer("NOW", "0001373715", "ServiceNow, Inc.", "7372"),
    "NVDA": VerifiedUsIssuer("NVDA", "0001045810", "NVIDIA CORP", "3674"),
    "PLD": VerifiedUsIssuer("PLD", "0001045609", "Prologis, Inc.", "6798"),
    "SBUX": VerifiedUsIssuer("SBUX", "0000829224", "STARBUCKS CORP", "5810"),
    "SNOW": VerifiedUsIssuer("SNOW", "0001640147", "Snowflake Inc.", "7372"),
    "VRTX": VerifiedUsIssuer("VRTX", "0000875320", "VERTEX PHARMACEUTICALS INC / MA", "2834"),
    "WMT": VerifiedUsIssuer("WMT", "0000104169", "Walmart Inc.", "5331"),
}

# Bilingual company names / aliases normalized to US ticker symbols.
# Names outside _VERIFIED_US_ISSUERS still require entity_resolver / SEC CIK lookup.
_CJK_ALIASES: tuple[tuple[str, str], ...] = (
    ("英伟达", "NVDA"),
    ("苹果公司", "AAPL"),
    ("苹果", "AAPL"),
    ("微软", "MSFT"),
    ("谷歌", "GOOGL"),
    ("字母表", "GOOGL"),
    ("亚马逊", "AMZN"),
    ("脸书", "META"),
    ("奈飞", "NFLX"),
    ("网飞", "NFLX"),
    ("超微半导体", "AMD"),
    ("博通", "AVGO"),
    ("沃尔玛", "WMT"),
    ("好市多", "COST"),
    ("开市客", "COST"),
    ("耐克", "NKE"),
    ("星巴克", "SBUX"),
    ("礼来", "LLY"),
    ("强生", "JNJ"),
    ("默沙东", "MRK"),
    ("摩根大通", "JPM"),
    ("普洛斯", "PLD"),
    ("赛富时", "CRM"),
    ("特斯拉", "TSLA"),
    ("英特尔", "INTC"),
    ("高通", "QCOM"),
    ("甲骨文", "ORCL"),
    ("美光科技", "MU"),
    ("美光", "MU"),
    ("奥多比", "ADBE"),
    ("帕兰蒂尔", "PLTR"),
)

_EN_ALIASES: tuple[tuple[str, str], ...] = (
    ("nvidia", "NVDA"),
    ("apple", "AAPL"),
    ("microsoft", "MSFT"),
    ("alphabet", "GOOGL"),
    ("google", "GOOGL"),
    ("amazon", "AMZN"),
    ("meta platforms", "META"),
    ("facebook", "META"),
    ("meta", "META"),
    ("netflix", "NFLX"),
    ("advanced micro devices", "AMD"),
    ("broadcom", "AVGO"),
    ("walmart", "WMT"),
    ("costco", "COST"),
    ("nike", "NKE"),
    ("starbucks", "SBUX"),
    ("eli lilly", "LLY"),
    ("johnson & johnson", "JNJ"),
    ("merck", "MRK"),
    ("vertex pharmaceuticals", "VRTX"),
    ("vertex", "VRTX"),
    ("jpmorgan chase", "JPM"),
    ("jpmorgan", "JPM"),
    ("jp morgan", "JPM"),
    ("prologis", "PLD"),
    ("salesforce", "CRM"),
    ("servicenow", "NOW"),
    ("snowflake", "SNOW"),
    ("tesla", "TSLA"),
    ("intel", "INTC"),
    ("qualcomm", "QCOM"),
    ("oracle", "ORCL"),
    ("adobe", "ADBE"),
    ("micron", "MU"),
    ("palantir", "PLTR"),
    ("crowdstrike", "CRWD"),
    ("datadog", "DDOG"),
    ("shopify", "SHOP"),
    ("airbnb", "ABNB"),
    ("uber", "UBER"),
)

_EN_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.I), ticker)
    for alias, ticker in _EN_ALIASES
)

# Deterministic earnings-review intent patterns (Chinese + English).
_EARNINGS_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("财报", re.compile(r"财报")),
    ("季报", re.compile(r"季报")),
    ("年报", re.compile(r"年报")),
    ("半年报", re.compile(r"半年报")),
    ("中报", re.compile(r"中报")),
    ("业绩", re.compile(r"业绩")),
    ("业绩会", re.compile(r"业绩会|业绩电话会|电话会")),
    ("盈利报告", re.compile(r"盈利报告|财务报告")),
    ("10-Q", re.compile(r"(?<![A-Za-z0-9])10-?Q(?![A-Za-z0-9])", re.I)),
    ("10-K", re.compile(r"(?<![A-Za-z0-9])10-?K(?![A-Za-z0-9])", re.I)),
    ("8-K", re.compile(r"(?<![A-Za-z0-9])8-?K(?![A-Za-z0-9])", re.I)),
    ("EX-99.1", re.compile(r"(?<![A-Za-z0-9])EX-?99\.1(?![A-Za-z0-9])", re.I)),
    ("earnings", re.compile(r"\bearnings(?:\s+(?:release|report|review|call|transcript|quality|preview|summary))?\b", re.I)),
    ("quarterly_results", re.compile(r"\bquarterly\s+(?:results?|report|earnings|filing)\b", re.I)),
    ("annual_results", re.compile(r"\bannual\s+(?:results?|report|earnings|filing)\b", re.I)),
    ("financial_results", re.compile(r"\bfinancial\s+results\b", re.I)),
    ("results_review", re.compile(r"\b(?:q[1-4]|fy\d{2,4})\s+(?:results?|review)\b", re.I)),
)

# Explicit exclusions.
_COMPARISON_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bvs\.?\b", re.I),
    re.compile(r"\bversus\b", re.I),
    re.compile(r"\bcompare\b|\bcomparison\b|\bcompared\s+(?:to|with)\b", re.I),
    re.compile(r"\bpeers?\b|\bagainst\s+peers\b|\brelative\s+to\b", re.I),
    re.compile(r"\bpk\b", re.I),
    re.compile(r"对比|比较|相比|同行|同业|横向|谁更|哪个更"),
)

_MACRO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:cpi|pce|ppi|pmi|gdp|gdi|fomc|ism|fed|ecb|boj|pboc)\b", re.I),
    re.compile(r"\b(?:federal\s+reserve|nonfarm|payrolls?|rate\s+decision|interest\s+rates?|monetary\s+policy|inflation|unemployment\s+rate|central\s+bank|treasury\s+yields?)\b", re.I),
    re.compile(r"宏观|非农|通胀|通缩|美联储|央行|利率决议|议息|降息|加息|失业率|社融|货币政策|国债收益率"),
)

_IPO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bipo\b", re.I),
    re.compile(r"\b(?:first\s+day\s+of\s+trading|first\s+trading\s+day|opening\s+day|listing\s+day|offer\s+price|debut)\b", re.I),
    re.compile(r"(?<![A-Za-z0-9])(?:s-1|f-1)(?![A-Za-z0-9])", re.I),
    re.compile(r"上市首日|首日表现|新股上市|发行价|招股书|招股"),
)

_NON_US_MARKER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"A股|Ａ股|沪市|深市|北交所|沪深|创业板|科创板|港股|H股|红筹|恒生|港交所|上交所|深交所|台股|日股|韩股|欧股|英股"),
    re.compile(r"\b(?:a-?shares?|h-?shares?|hkex|sehk|sse|szse|bjse|twse|krx|tse|lse|tsx|asx|xetra|hong\s+kong\s+stock|shanghai\s+stock|shenzhen\s+stock)\b", re.I),
    re.compile(r"(?<![A-Za-z0-9])\d{1,6}\s*\.\s*(?:HK|SS|SH|SZ|BJ|TW|TWO|KS|KQ|T|TO|L|AX|DE|NS|BO)(?![A-Za-z0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9])(?:HK|HKEX|SEHK|SH|SZ|BJ|SSE|SZSE|BJSE)\s*[:.]\s*\d{1,6}(?![A-Za-z0-9])", re.I),
    re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{1,8}\.(?:HK|SS|SH|SZ|BJ|TW|TWO|KS|KQ|L|TO|AX|DE|NS|BO)(?![A-Za-z0-9])", re.I),
)

_SIX_DIGIT_TOKEN_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_DOLLAR_OR_EXCHANGE_TICKER_RE = re.compile(
    r"(?:\$(?P<dollar>[A-Z]{1,5}(?:\.[AB])?)|"
    r"(?:NASDAQ|NYSE|AMEX|ARCA)\s*[:：]\s*(?P<ex>[A-Z]{1,5}(?:\.[AB])?))"
)
_BARE_UPPER_TICKER_RE = re.compile(r"(?<![A-Za-z0-9.$])([A-Z]{2,5}(?:\.[AB])?)(?![A-Za-z0-9.])")
_EXPLICIT_CIK_RE = re.compile(r"\bCIK\s*[:#-]?\s*0*(\d{1,10})\b", re.I)
_TITLE_WORD_RE = re.compile(r"\b([A-Z][a-zA-Z0-9&.-]{1,30}(?:\s+[A-Z][a-zA-Z0-9&.-]{1,20}){0,2})\b")

_TICKER_STOPWORDS: frozenset[str] = frozenset({
    "Q1", "Q2", "Q3", "Q4", "FY", "YOY", "QOQ", "MOM", "TTM", "NTM", "LTM", "YTD",
    "US", "USA", "SEC", "EDGAR", "CIK", "IRS", "GAAP", "IFRS", "EPS", "FCF", "OCF",
    "DCF", "WACC", "ROI", "ROE", "ROA", "ROIC", "ROTCE", "PE", "PB", "PS", "EV",
    "EBIT", "EBITDA", "KPI", "ARR", "NRR", "DBNR", "ARPU", "TAC", "SSSG", "LOE",
    "NIM", "CET1", "FFO", "AFFO", "RPO", "CRPO", "SBC", "DSO", "DIO", "DPO", "CCC",
    "COGS", "SGA", "RD", "MDA", "EX", "ITEM", "FORM", "NOTE", "NOTES",
    "AI", "GPU", "CPU", "LLM", "API", "SDK", "PDF", "HTML", "CSV", "XLSX", "JSON",
    "BYOK", "OK", "ETF", "REIT", "ADR", "USD", "CNY", "RMB", "HKD", "EUR", "JPY",
    "KRW", "GBP", "HK", "CN", "SS", "SZ", "BJ", "NYSE", "NASDAQ", "AMEX",
    "CEO", "CFO", "COO", "CTO", "CIO", "IR", "PR", "QA", "FAQ",
    "THE", "AND", "FOR", "NOT", "ARE", "WAS", "WERE", "DID", "DOES", "HAS", "HAVE",
    "CAN", "WILL", "ITS", "WITH", "FROM", "INTO", "OVER", "LAST", "LATEST", "NEW",
    "TOP", "VS", "PK", "INC", "CORP", "CO", "LTD", "LLC", "PLC", "HOLDINGS", "GROUP",
    "WHAT", "HOW", "WHY", "WHO", "WHEN", "WHERE", "WHICH", "THIS", "THAT", "CHECK",
    "ANALYZE", "ANALYSE", "REVIEW", "REPORT", "EARNINGS", "QUARTER", "QUARTERLY",
    "ANNUAL", "RESULTS", "FILING", "FILINGS", "GUIDANCE", "QUALITY", "DEEP", "DIVE",
    "CALL", "TRANSCRIPT", "SUMMARY", "SUMMARIZE", "EXPLAIN", "TELL", "PLEASE", "READ",
    "MARGIN", "MARGINS", "REVENUE", "PROFIT", "CASH", "FLOW", "SEGMENT", "SEGMENTS",
    "VALUATION", "FORENSICS", "BEAR", "CASE", "BUY", "SIDE", "FULL", "REAL", "LIVE",
})

_NON_COMPANY_PHRASE_WORDS: frozenset[str] = frozenset(
    w.casefold() for w in _TICKER_STOPWORDS
) | frozenset({
    "a", "an", "the", "latest", "recent", "current", "most", "up", "to", "date",
    "year", "fiscal", "quarter", "financial", "financials", "performance", "outlook",
    "release", "statement", "statements", "balance", "sheet", "income", "breakdown",
    "analysis", "preview", "recap", "update", "commentary", "highlights", "drivers",
    "risk", "risks", "redline", "diff", "table", "tables", "company", "stock",
    "equity", "shares", "share", "market", "us", "american", "chinese",
})


class EarningsSquadRouteDecision(BaseModel):
    """Deterministic routing outcome for default research run entry."""

    model_config = ConfigDict(frozen=True)

    should_route: bool
    run_mode: RunMode
    reason: str
    ticker: str | None = None
    cik: str | None = None
    entity_id: str | None = None
    company_name: str | None = None
    matched_intent_terms: tuple[str, ...] = ()
    matched_candidates: tuple[str, ...] = ()
    sources_mode: str = "auto"


def _reject(
    reason: str,
    *,
    sources_mode: str = "auto",
    matched_intent_terms: Sequence[str] = (),
    matched_candidates: Sequence[str] = (),
) -> EarningsSquadRouteDecision:
    return EarningsSquadRouteDecision(
        should_route=False,
        run_mode="legacy_single_loop",
        reason=reason,
        matched_intent_terms=tuple(matched_intent_terms),
        matched_candidates=tuple(matched_candidates),
        sources_mode=sources_mode,
    )


def derive_sources_mode(
    request: Any,
    *,
    has_attached_sources: bool = False,
) -> str:
    """Translate ``ResearchRunRequest`` source/retrieval flags into a routing ``sources_mode``."""
    supplied: set[str] = set(getattr(request, "model_fields_set", ()) or ())
    selected = getattr(request, "selected_source_ids", None)
    if selected is not None and len(selected) == 0:
        return "empty"

    auto_fetch = getattr(request, "auto_fetch_official", None)
    discover = getattr(request, "discover_sources", None)
    if "auto_fetch_official" in supplied and auto_fetch is False:
        return "private_only" if (has_attached_sources or bool(selected)) else "empty"
    if "discover_sources" in supplied and discover is False and "auto_fetch_official" not in supplied:
        return "private_only" if (has_attached_sources or bool(selected)) else "empty"
    if selected is not None and len(selected) > 0:
        return "selected"
    return "auto"


def _looks_like_cn_stock_code(token: str) -> bool:
    parsed = cn_helpers.parse_cn_code(token)
    if parsed is None:
        return False
    # Avoid misclassifying YYYYMM numeric tokens (e.g. 202512) as SZSE codes.
    if token.startswith(("19", "20")):
        month = int(token[4:6])
        if 1 <= month <= 12:
            return False
    return True


def _has_non_us_marker(question: str) -> bool:
    if any(pat.search(question) for pat in _NON_US_MARKER_PATTERNS):
        return True
    for match in _SIX_DIGIT_TOKEN_RE.finditer(question):
        token = match.group(1)
        # Do not treat an explicitly labelled CIK as a 6-digit CN code.
        prefix = question[max(0, match.start() - 10) : match.start()]
        if re.search(r"CIK\s*[:#-]?\s*$", prefix, re.I):
            continue
        if _looks_like_cn_stock_code(token):
            return True
    return False


def _is_period_or_form_token(token: str) -> bool:
    return bool(re.fullmatch(r"(?:FY\d{2,4}|Q[1-4](?:FY\d{2,4})?)", token, re.I))


@dataclass(frozen=True)
class _ExtractedCandidate:
    query: str
    raw_label: str
    canonical_hint: str | None = None


def _extract_candidates(question: str) -> list[_ExtractedCandidate]:
    """Extract ordered, deduplicated company/ticker/CIK candidate queries from ``question``."""
    found: list[_ExtractedCandidate] = []
    seen_queries: set[str] = set()

    def add(query: str, raw_label: str, canonical_hint: str | None = None) -> None:
        key = (canonical_hint or query).strip().upper()
        if not key or key in seen_queries:
            return
        seen_queries.add(key)
        found.append(_ExtractedCandidate(query=query.strip(), raw_label=raw_label, canonical_hint=canonical_hint))

    # 1. Explicit 10-digit (or shorter) CIK references.
    for match in _EXPLICIT_CIK_RE.finditer(question):
        digits = match.group(1)
        if digits.isdigit() and int(digits) > 0:
            norm = cik10(digits)
            add(norm, f"CIK {norm}", canonical_hint=f"CIK:{norm}")

    # 2. Chinese company aliases.
    for alias, ticker in _CJK_ALIASES:
        if alias in question:
            add(ticker, alias, canonical_hint=ticker)

    # 3. English company aliases (case-insensitive word boundaries).
    for pattern, ticker in _EN_ALIAS_PATTERNS:
        m = pattern.search(question)
        if m:
            add(ticker, m.group(0), canonical_hint=ticker)

    # 4. Explicit $TICKER or EXCHANGE:TICKER notations.
    for match in _DOLLAR_OR_EXCHANGE_TICKER_RE.finditer(question):
        sym = (match.group("dollar") or match.group("ex") or "").upper()
        if sym and sym not in _TICKER_STOPWORDS and not _is_period_or_form_token(sym):
            add(sym, match.group(0), canonical_hint=sym)

    # 5. Bare uppercase ticker tokens (2-5 chars, preserving case from original question).
    for match in _BARE_UPPER_TICKER_RE.finditer(question):
        sym = match.group(1).upper()
        if sym in _TICKER_STOPWORDS or _is_period_or_form_token(sym):
            continue
        add(sym, sym, canonical_hint=sym)

    # 6. Fallback phrase candidates when no ticker/alias/CIK matched (allows entity_resolver
    #    to resolve arbitrary US company names such as "Palantir" or "CrowdStrike").
    if not found:
        for match in _TITLE_WORD_RE.finditer(question):
            phrase = match.group(1).strip(" .,:;!?()[]")
            words = [w.strip(".,:;!?()[]'\"") for w in phrase.split()]
            cleaned_words = [w for w in words if w and w.casefold() not in _NON_COMPANY_PHRASE_WORDS and not _is_period_or_form_token(w)]
            if not cleaned_words:
                continue
            candidate_phrase = " ".join(cleaned_words)
            if len(candidate_phrase) >= 2:
                add(candidate_phrase, candidate_phrase)

    return found


@dataclass(frozen=True)
class _ResolvedUsEquity:
    ticker: str
    cik: str
    entity_id: str
    company_name: str | None


def _valid_ten_digit_cik(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper().removeprefix("CIK")
    if not re.fullmatch(r"\d{10}", cleaned) or int(cleaned) == 0:
        return None
    return cleaned


def _coerce_candidates(result: Any) -> tuple[bool, list[EntityCandidate]]:
    """Normalize outputs from ``resolve_entity`` or custom ``entity_resolver`` callables.

    Returns ``(errored_or_unavailable, candidates)``.
    """
    if result is None:
        return False, []
    if isinstance(result, ToolResult):
        if result.status in ("failed", "denied", "rate_limited"):
            return True, []
        if result.status == "empty":
            return False, []
        items = list(result.data or [])
        return False, [item for item in items if isinstance(item, EntityCandidate)]
    if isinstance(result, EntityCandidate):
        return False, [result]
    if isinstance(result, EntityRef):
        return False, [EntityCandidate(entity=result, match_basis="provider_search", unique=True)]
    if isinstance(result, Mapping):
        if "entity" in result:
            return False, [EntityCandidate.model_validate(result)]
        if "entity_id" in result or "cik" in result or "symbol" in result:
            identifiers = dict(result.get("identifiers") or {})
            if isinstance(result.get("cik"), str) and "cik" not in identifiers:
                identifiers["cik"] = result["cik"]
            cik_val = identifiers.get("cik")
            eid = str(result.get("entity_id") or (f"cik:{cik_val}" if cik_val else result.get("symbol") or "unknown"))
            ref = EntityRef(
                entity_id=eid,
                company_name=result.get("company_name"),
                symbol=result.get("symbol"),
                exchange=result.get("exchange"),
                currency=result.get("currency"),
                asset_type=result.get("asset_type"),
                identifiers=identifiers,
            )
            return False, [EntityCandidate(entity=ref, match_basis=result.get("match_basis", "provider_search"), unique=bool(result.get("unique", True)))]
        return False, []
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        out: list[EntityCandidate] = []
        for item in result:
            _, sub = _coerce_candidates(item)
            out.extend(sub)
        return False, out
    return False, []


def _extract_us_equity_from_candidate(cand: EntityCandidate) -> tuple[bool, _ResolvedUsEquity | None]:
    """Return ``(is_eligible_us_equity, resolved_or_none)``.

    Returns ``(False, None)`` when the candidate explicitly points to a non-US exchange,
    non-equity asset type, or lacks a valid 10-digit nonzero SEC CIK.
    """
    ref = cand.entity
    exchange = ref.exchange.strip().upper() if isinstance(ref.exchange, str) and ref.exchange.strip() else None
    if exchange is not None and exchange not in US_EXCHANGES:
        return False, None
    asset_type = ref.asset_type.strip().lower() if isinstance(ref.asset_type, str) and ref.asset_type.strip() else None
    if asset_type is not None and asset_type != "equity":
        return False, None
    symbol = ref.symbol.strip().upper() if isinstance(ref.symbol, str) and ref.symbol.strip() else None
    if symbol and re.search(r"\.(?:HK|SS|SH|SZ|BJ|TW|TWO|KS|KQ|T|TO|L|AX|DE|NS|BO)$", symbol, re.I):
        return False, None
    parsed_eid = parse_entity_id(ref.entity_id)
    if parsed_eid["exchange"] and parsed_eid["exchange"].upper() not in US_EXCHANGES:
        return False, None
    raw_cik = ref.identifiers.get("cik") or parsed_eid.get("cik")
    valid_cik = _valid_ten_digit_cik(raw_cik)
    if valid_cik is None:
        return False, None
    if not symbol:
        return False, None
    return True, _ResolvedUsEquity(
        ticker=symbol,
        cik=valid_cik,
        entity_id=ref.entity_id if ref.entity_id else f"cik:{valid_cik}",
        company_name=ref.company_name,
    )


def _resolve_single_candidate_via_resolver(
    query: str,
    resolver: Callable[[str], Any],
) -> tuple[str, _ResolvedUsEquity | None]:
    """Resolve one query via an injected or default ``entity_resolver``.

    Returns ``(status, resolved)`` where status is one of:
    ``"ok"``, ``"ambiguous"``, ``"non_us_or_missing_cik"``, ``"unavailable"``, ``"no_match"``.
    """
    try:
        raw = resolver(query)
    except Exception:
        return "unavailable", None
    errored, cands = _coerce_candidates(raw)
    if errored and not cands:
        return "unavailable", None
    if not cands:
        return "no_match", None

    # First check if there are multiple candidates and whether a single exact match disambiguates them
    # (SEC company_tickers substring search often returns extra name_contains matches alongside ticker_exact).
    q_upper = query.strip().upper()
    q_digits = q_upper.removeprefix("CIK")
    exact_cands = [
        c
        for c in cands
        if c.match_basis in ("cik_exact", "ticker_exact", "symbol_exact")
        or (c.entity.symbol and c.entity.symbol.strip().upper() == q_upper)
        or (
            q_digits.isdigit()
            and (c.entity.identifiers.get("cik") or "").lstrip("0") == q_digits.lstrip("0")
        )
    ]

    if len(cands) > 1:
        if len(exact_cands) == 1:
            chosen = [exact_cands[0]]
        elif len(exact_cands) > 1:
            return "ambiguous", None
        else:
            all_us_ciks: list[str] = []
            for cand in cands:
                ok, eq = _extract_us_equity_from_candidate(cand)
                if not ok or eq is None:
                    return "ambiguous", None
                all_us_ciks.append(eq.cik)
            if len(set(all_us_ciks)) != 1:
                return "ambiguous", None
            chosen = [cands[0]]
    else:
        chosen = [cands[0]]

    ok, resolved = _extract_us_equity_from_candidate(chosen[0])
    if not ok or resolved is None:
        return "non_us_or_missing_cik", None
    return "ok", resolved


def detect_us_earnings_squad_route(
    question: str,
    *,
    sources_mode: str | None = "auto",
    allow_auto_squad: bool = True,
    entity_resolver: Callable[[str], Any] | None = None,
    sec_adapter: SecEdgarAdapter | None = None,
    discover_sources: bool | None = None,
    auto_fetch_official: bool | None = None,
    selected_source_ids: Sequence[str] | None = None,
) -> EarningsSquadRouteDecision:
    """Determine whether ``question`` should auto-route to the US Earnings Buy-Side Squad."""
    normalized_mode = (sources_mode or "auto").strip().lower()
    if not allow_auto_squad:
        return _reject("auto_squad_disabled", sources_mode=normalized_mode)

    if normalized_mode in _OPT_OUT_SOURCES_MODES:
        return _reject("external_retrieval_disabled", sources_mode=normalized_mode)
    if normalized_mode not in _ALLOWED_SOURCES_MODES:
        return _reject("unsupported_sources_mode", sources_mode=normalized_mode)
    if selected_source_ids is not None and len(selected_source_ids) == 0:
        return _reject("explicit_empty_sources", sources_mode="empty")
    if auto_fetch_official is False:
        return _reject("official_fetch_disabled", sources_mode=normalized_mode)
    if discover_sources is False and auto_fetch_official is None:
        return _reject("discovery_and_official_disabled", sources_mode=normalized_mode)

    q = (question or "").strip()
    if not q:
        return _reject("empty_question", sources_mode=normalized_mode)

    if any(pat.search(q) for pat in _COMPARISON_PATTERNS):
        return _reject("comparison_excluded", sources_mode=normalized_mode)
    if any(pat.search(q) for pat in _MACRO_PATTERNS):
        return _reject("macro_excluded", sources_mode=normalized_mode)
    if any(pat.search(q) for pat in _IPO_PATTERNS):
        return _reject("ipo_excluded", sources_mode=normalized_mode)
    if _has_non_us_marker(q):
        return _reject("non_us_market_excluded", sources_mode=normalized_mode)

    matched_intent = tuple(label for label, pat in _EARNINGS_INTENT_PATTERNS if pat.search(q))
    if not matched_intent:
        return _reject("no_earnings_intent", sources_mode=normalized_mode)

    candidates = _extract_candidates(q)
    matched_labels = tuple(c.raw_label for c in candidates)
    if not candidates:
        return _reject(
            "no_company_detected",
            sources_mode=normalized_mode,
            matched_intent_terms=matched_intent,
        )

    effective_resolver = entity_resolver
    if effective_resolver is None and sec_adapter is not None:
        effective_resolver = lambda query: resolve_entity(
            query, market="US", asset_type="equity", sec=sec_adapter
        )

    resolved_by_cik: dict[str, _ResolvedUsEquity] = {}
    failure_reasons: list[str] = []

    for cand in candidates:
        if entity_resolver is not None:
            status, resolved = _resolve_single_candidate_via_resolver(cand.query, entity_resolver)
        else:
            verified = _VERIFIED_US_ISSUERS.get(cand.query.upper())
            if verified is None and cand.query.upper().startswith("CIK:"):
                cik_val = _valid_ten_digit_cik(cand.query[4:])
                verified = next((v for v in _VERIFIED_US_ISSUERS.values() if v.cik == cik_val), None)
            elif verified is None and re.fullmatch(r"\d{10}", cand.query):
                verified = next((v for v in _VERIFIED_US_ISSUERS.values() if v.cik == cand.query), None)

            if verified is not None:
                status = "ok"
                resolved = _ResolvedUsEquity(
                    ticker=verified.symbol,
                    cik=verified.cik,
                    entity_id=f"cik:{verified.cik}",
                    company_name=verified.company_name,
                )
            elif effective_resolver is not None:
                status, resolved = _resolve_single_candidate_via_resolver(cand.query, effective_resolver)
            else:
                status, resolved = "unavailable", None

        if status == "ok" and resolved is not None:
            resolved_by_cik[resolved.cik] = resolved
        else:
            failure_reasons.append(status)

    if len(resolved_by_cik) > 1:
        return _reject(
            "multiple_companies",
            sources_mode=normalized_mode,
            matched_intent_terms=matched_intent,
            matched_candidates=matched_labels,
        )

    if len(resolved_by_cik) == 1:
        # If one candidate resolved AND another unrelated ticker/alias failed as non-US/ambiguous,
        # only route when there aren't multiple competing company mentions.
        if any(r == "non_us_or_missing_cik" for r in failure_reasons):
            return _reject(
                "multiple_companies",
                sources_mode=normalized_mode,
                matched_intent_terms=matched_intent,
                matched_candidates=matched_labels,
            )
        winner = next(iter(resolved_by_cik.values()))
        return EarningsSquadRouteDecision(
            should_route=True,
            run_mode="earnings_squad",
            reason="matched_us_single_company_earnings",
            ticker=winner.ticker,
            cik=winner.cik,
            entity_id=winner.entity_id,
            company_name=winner.company_name,
            matched_intent_terms=matched_intent,
            matched_candidates=matched_labels,
            sources_mode=normalized_mode,
        )

    if "ambiguous" in failure_reasons:
        final_reason = "ambiguous_entity"
    elif "non_us_or_missing_cik" in failure_reasons:
        final_reason = "non_us_or_missing_cik"
    elif "unavailable" in failure_reasons:
        final_reason = "entity_resolution_unavailable"
    else:
        final_reason = "no_company_detected"
    return _reject(
        final_reason,
        sources_mode=normalized_mode,
        matched_intent_terms=matched_intent,
        matched_candidates=matched_labels,
    )


__all__ = [
    "EarningsSquadRouteDecision",
    "RunMode",
    "US_EXCHANGES",
    "derive_sources_mode",
    "detect_us_earnings_squad_route",
]
