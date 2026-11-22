"""Fixed-endpoint web-search adapters. Results are unverified discovery leads.

A probe performs one real search and may incur provider charges. No credentials or
provider response bodies are included in failures. Provider settings do not grant
permission to retrieve result pages.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..fin_data.models import Cost, Coverage, Permissions, Provenance, ToolResult
from ..glm_mcp_search import SearchCandidate
from ..openai_compatible import _default_transport
from ..phase3 import ProviderFailure
from .gdelt import NewsLead


@dataclass(frozen=True)
class ProviderSpec:
    label: str
    endpoint: str
    signup_url: str
    docs_url: str
    region: str = "global"
    notes: str = "Search requests may incur charges."


PROVIDERS = {
    "brave": ProviderSpec("Brave Search", "https://api.search.brave.com/res/v1/web/search", "https://brave.com/search/api/", "https://api.search.brave.com/app/documentation/web-search/get-started"),
    "tavily": ProviderSpec("Tavily", "https://api.tavily.com/search", "https://tavily.com/", "https://docs.tavily.com/documentation/api-reference/endpoint/search"),
    "exa": ProviderSpec("Exa", "https://api.exa.ai/search", "https://exa.ai/", "https://exa.ai/docs/reference/search"),
    "parallel": ProviderSpec("Parallel", "https://api.parallel.ai/v1/search", "https://platform.parallel.ai/", "https://docs.parallel.ai/api-reference/search/web"),
    "qianfan": ProviderSpec("百度千帆搜索", "https://qianfan.baidubce.com/v2/ai_search/web_search", "https://cloud.baidu.com/", "https://cloud.baidu.com/doc/qianfan-api/s/wmwbk70bv", "china"),
    "bocha": ProviderSpec("博查搜索", "https://api.bochaai.com/v1/web-search", "https://open.bochaai.com/", "https://open.bochaai.com/", "china"),
}


def _failure(code: str, message: str, retryable: bool = False) -> ProviderFailure:
    return ProviderFailure(code, message, retryable=retryable)


def _error(status: int, code: Any = None) -> ProviderFailure:
    # Only inspect documented codes, never echo untrusted bodies or messages.
    c = str(code or "").upper()
    if c in {"QUOTA_USER_DAILY_FREE", "BILLING_INSUFFICIENT_BALANCE", "INSUFFICIENT_QUOTA", "QUOTA_EXCEEDED"} or status == 402:
        return _failure("search_quota_exhausted", "Search provider quota or balance is exhausted.")
    if status == 429 or c == "RATE_LIMIT_SEARCH_QPS":
        return _failure("search_rate_limited", "Search provider rate limit reached.", True)
    if status in (401, 403) or c in {"216003", "INVALID_API_KEY", "UNAUTHORIZED"}:
        return _failure("search_auth_failed", "Search provider rejected credentials or access.")
    return _failure("search_provider_error", "Search provider request failed.", status >= 500)


def _text(value: Any, maximum: int) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _published(value: Any) -> date | datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        # A timezone-free date/time is not an unambiguous instant; keep its date only.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.date()
    except ValueError:
        return None


def _request(provider_id: str, key: str, query: str, limit: int) -> tuple[str, str, dict[str, str], bytes]:
    spec = PROVIDERS[provider_id]
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    method, url = "POST", spec.endpoint
    if provider_id == "brave":
        method = "GET"
        url += "?" + urlencode({"q": query, "count": min(limit, 20)})
        headers["X-Subscription-Token"] = key
        payload = None
    elif provider_id == "tavily":
        headers["Authorization"] = "Bearer " + key
        payload = {"query": query, "max_results": min(limit, 20), "search_depth": "basic", "include_published_date": True}
    elif provider_id == "exa":
        headers["x-api-key"] = key
        payload = {"query": query, "numResults": limit, "type": "auto", "contents": {"highlights": True}}
    elif provider_id == "parallel":
        headers["x-api-key"] = key
        payload = {"search_queries": [query], "objective": query, "mode": "basic", "max_chars_total": 12000}
    elif provider_id == "qianfan":
        headers["Authorization"] = "Bearer " + key
        payload = {"messages": [{"role": "user", "content": query}], "search_source": "baidu_search_v2", "edition": "standard", "resource_type_filter": [{"type": "web", "top_k": limit}]}
    else:
        headers["Authorization"] = "Bearer " + key
        payload = {"query": query, "count": limit, "summary": True}
    return method, url, headers, json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""


def _results(provider_id: str, body: dict[str, Any]) -> list[Any]:
    if provider_id == "brave":
        obj = body.get("web")
        value = obj.get("results") if isinstance(obj, dict) else None
        if obj is None and body.get("type") == "search":
            value = []
    elif provider_id == "qianfan":
        value = body.get("references", [])
    elif provider_id == "bocha":
        data = body.get("data", body)
        pages = data.get("webPages") if isinstance(data, dict) else None
        value = pages.get("value") if isinstance(pages, dict) else None
    else:
        value = body.get("results")
    if not isinstance(value, list):
        raise _failure("search_invalid_response", "Search provider returned an invalid response.")
    return value


def search_provider(provider_id: str, api_key: str, query: str, *, limit: int = 5, timeout: float = 10, transport: Any = None) -> tuple[SearchCandidate, ...]:
    if provider_id not in PROVIDERS:
        raise _failure("search_provider_unknown", "Unknown search provider.")
    if not isinstance(api_key, str) or not api_key.strip():
        raise _failure("search_provider_unconfigured", "Search provider key is not configured.")
    if not isinstance(query, str) or not query.strip() or len(query) > 4000:
        raise _failure("search_query_invalid", "Search query is invalid.")
    limit = max(1, min(int(limit), 20))
    timeout = max(0.1, min(float(timeout), 60.0))
    method, url, headers, payload = _request(provider_id, api_key.strip(), query.strip(), limit)
    try:
        response = (transport or _default_transport)(method, url, headers, payload, timeout)
    except Exception:
        raise _failure("search_provider_unavailable", "Search provider could not be reached.", True) from None
    try:
        body = json.loads(response.body)
    except (ValueError, TypeError):
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if response.status_code < 200 or response.status_code >= 300:
        raise _error(response.status_code, code)
    if not isinstance(body, dict):
        raise _failure("search_invalid_response", "Search provider returned an invalid response.")
    if (provider_id == "qianfan" and code not in (None, 0, "0")) or (provider_id == "bocha" and code not in (None, 0, "0", 200, "200")) or body.get("error"):
        raise _error(response.status_code, code)
    out = []
    for item in _results(provider_id, body):
        if not isinstance(item, dict):
            continue
        url_value = _text(item.get("url"), 4096)
        parts = urlsplit(url_value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            continue
        title = _text(item.get("title") or item.get("name"), 1000)
        if not title:
            continue
        excerpt = item.get("description") or item.get("content") or item.get("snippet") or item.get("summary") or item.get("highlights") or item.get("excerpts")
        if isinstance(excerpt, list):
            excerpt = " ".join(x for x in excerpt if isinstance(x, str))
        published = item.get("publishedDate") or item.get("published_date") or item.get("publish_date") or item.get("datePublished") or item.get("date") or item.get("page_age")
        publisher = _text(item.get("website") or item.get("siteName"), 500) or None
        out.append(SearchCandidate(title=title, url=url_value, excerpt=_text(excerpt, 4000), publisher=publisher, published_at=_published(published), independently_reviewable=True))
        if len(out) >= limit:
            break
    return tuple(out)


def probe_provider(provider_id: str, api_key: str, *, timeout: float = 10, transport: Any = None) -> dict[str, Any]:
    """Send one search request; success permits an empty results list."""
    try:
        search_provider(provider_id, api_key, "public company annual report", limit=1, timeout=timeout, transport=transport)
    except ProviderFailure as error:
        return {"ok": False, "code": error.code, "message": str(error)}
    return {"ok": True, "code": "ok", "message": "Search provider responded successfully."}


class ManagedNewsProvider:
    supports_time_filter = False

    def __init__(self, provider_id: str, api_key: str, *, timeout: float = 10, transport: Any = None):
        self.name, self.api_key, self.timeout, self.transport = provider_id, api_key, timeout, transport

    def search(self, query, *, since=None, until=None, languages=None, limit=10) -> ToolResult:
        from .search import guess_language, limited_failure_result
        now = datetime.now(timezone.utc)
        prov = Provenance(provider=self.name, endpoint=self.name, retrieved_at=now)
        try:
            candidates = search_provider(self.name, self.api_key, query, limit=limit, timeout=self.timeout, transport=self.transport)
        except ProviderFailure as error:
            calls = 0 if error.code in {"search_provider_unknown", "search_provider_unconfigured", "search_query_invalid"} else 1
            return limited_failure_result(error, prov, Cost(provider_calls=calls))
        leads = []
        for c in candidates:
            published = c.published_at
            if isinstance(published, datetime):
                moment, precision = published, "datetime"
            elif isinstance(published, date):
                moment, precision = datetime(published.year, published.month, published.day, tzinfo=timezone.utc), "date"
            else:
                moment, precision = None, None
            leads.append(NewsLead(url=c.url, title=c.title, excerpt=c.excerpt or None, publisher=c.publisher,
                                  language=guess_language(c.title), published_at=moment, published_precision=precision, providers=[self.name]))
        if not leads:
            return ToolResult(status="empty", reason="no_results", message="Search returned no candidates.", provenance=prov, cost=Cost(provider_calls=1))
        return ToolResult(status="ok", reason="ok", coverage=Coverage(rows=len(leads)), provenance=prov, cost=Cost(provider_calls=1),
                          permissions=Permissions(store=True, display=True, model_input=True, export=False), data=leads)
