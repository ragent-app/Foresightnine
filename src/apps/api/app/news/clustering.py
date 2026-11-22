"""Duplicate and syndication clustering for news leads and fetched articles.

Items are merged when they share a canonical URL, or when titles (and bodies, when both are available) are
near-duplicates by 64-bit SimHash over text shingles. ``independent_source_count`` counts distinct *origins*: a
wire story republished by several outlets (``(Reuters) -`` datelines) counts once, under the agency. The result is
deterministic: no Python ``hash()``, stable ordering, ids derived from the representative's canonical URL.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_ITEMS = 1500
_MAX_TEXT = 20_000
MIN_FULL_BODY_CHARS = 200
BODY_HAMMING = 4
BODY_HAMMING_WITH_TITLE = 10
TITLE_SIMILARITY = 0.85
TITLE_ONLY_WINDOW_SECONDS = 3 * 86400

_TRACKING_EXACT = frozenset({"fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
                             "ref_url", "cmpid", "ocid", "ncid", "spm", "_ga", "srsltid", "cid", "taid", "amp", "outputtype",
                             "output", "ito", "source", "campaign", "smid", "smtyp", "partner", "referrer", "guccounter"})
_MOBILE_PREFIXES = ("www.", "m.", "mobile.", "amp.", "wap.")
# Aggregators/portals that republish other outlets' stories; they never add an independent source on their own.
_AGGREGATORS = frozenset({"yahoo.com", "msn.com", "flipboard.com", "newsbreak.com", "dailyhunt.in", "smartnews.com",
                          "ground.news", "newsnow.co.uk", "investing.com", "marketscreener.com", "benzinga.com",
                          "seekingalpha.com", "toutiao.com", "sohu.com", "sina.com.cn", "163.com", "qq.com"})
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "gov", "edu", "ac"})
# agency key -> (host suffixes owned by the agency, dateline regexes over the first part of the body)
_AGENCIES: dict[str, tuple[tuple[str, ...], re.Pattern[str]]] = {
    "reuters": (("reuters.com",), re.compile(r"\(\s*reuters\s*\)\s*[-–—]|路透(?:社)?[^\s]{0,12}电", re.I)),
    "ap": (("apnews.com",), re.compile(r"\(\s*(?:ap|associated press)\s*\)\s*[-–—]|美联社[^\s]{0,12}电", re.I)),
    "afp": (("afp.com",), re.compile(r"\(\s*afp\s*\)\s*[-–—]|法新社[^\s]{0,12}电", re.I)),
    "bloomberg": (("bloomberg.com",), re.compile(r"\(\s*bloomberg\s*\)\s*[-–—]|彭博[^\s]{0,12}电", re.I)),
    "xinhua": (("xinhuanet.com", "news.cn"), re.compile(r"新华社[^\s]{0,12}电")),
    "dowjones": (("dowjones.com",), re.compile(r"\(\s*dow jones(?: newswires)?\s*\)\s*[-–—]", re.I)),
}
_TITLE_SUFFIX = re.compile(r"\s+[-–—|·:]\s+[^-–—|·:]{2,40}$")
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


@dataclass(frozen=True)
class ClusterInput:
    id: str
    url: str
    title: str = ""
    text: str | None = None
    published_at: datetime | None = None
    seen_at: datetime | None = None
    domain: str | None = None


@dataclass(frozen=True)
class NewsCluster:
    cluster_id: str
    representative_id: str
    member_ids: tuple[str, ...]
    independent_source_count: int
    independent_sources: tuple[str, ...]
    syndicated_from: tuple[str, ...] = ()


@dataclass
class ClusterResult:
    clusters: list[NewsCluster]
    item_cluster: dict[str, str]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- URL canonicalisation

def canonical_news_url(url: str) -> str:
    """Strip tracking parameters, fragments, mobile/AMP variants, default ports and trailing slashes."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except ValueError:
        return raw
    if parts.scheme.lower() not in ("http", "https") or not host:
        return raw
    for prefix in _MOBILE_PREFIXES:
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
            break
    path = parts.path or "/"
    path = re.sub(r"(?:/amp|\.amp)(?:/|\.html)?$", "", path, flags=re.I) if path.lower() != "/amp" else "/"
    path = re.sub(r"^/amp/", "/", path, flags=re.I)
    path = re.sub(r"/index\.(?:html?|php|aspx?)$", "/", path, flags=re.I)
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_EXACT]
    query.sort()
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    return urlunsplit(("https", netloc, path or "/", urlencode(query), ""))


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def registrable_domain(host: str) -> str:
    labels = [l for l in host.lower().split(".") if l]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# ---------------------------------------------------------------- text fingerprints

def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(re.sub(r"[^\w\s']", " ", text).replace("_", " ").split())


def _shingles(text: str, size: int = 3) -> list[str]:
    norm = _normalize(text[:_MAX_TEXT])
    if not norm:
        return []
    cjk = len(_CJK.findall(norm))
    if cjk >= max(8, len(norm) // 4):
        chars = norm.replace(" ", "")
        return [chars[i:i + size] for i in range(max(1, len(chars) - size + 1))]
    words = _WORD.findall(norm)
    if len(words) < size:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + size]) for i in range(len(words) - size + 1)]


def simhash(text: str) -> int | None:
    shingles = _shingles(text)
    if not shingles:
        return None
    vector = [0] * 64
    for sh in shingles:
        h = int.from_bytes(hashlib.blake2b(sh.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            vector[bit] += 1 if (h >> bit) & 1 else -1
    return sum(1 << bit for bit in range(64) if vector[bit] > 0)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _title_tokens(title: str) -> list[str]:
    norm = _normalize(_TITLE_SUFFIX.sub("", title or "").strip())
    cjk = len(_CJK.findall(norm))
    if cjk >= max(4, len(norm) // 3):
        chars = norm.replace(" ", "")
        return [chars[i:i + 2] for i in range(max(1, len(chars) - 1))]
    return _WORD.findall(norm)


def title_similarity(a: str, b: str) -> float:
    ta, tb = set(_title_tokens(a)), set(_title_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _numbers(title: str) -> frozenset[str]:
    return frozenset(re.findall(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", title or "")))


# ---------------------------------------------------------------- clustering

@dataclass
class _Prepared:
    item: ClusterInput
    canonical: str
    host: str
    sim: int | None
    title_ok: bool
    when: datetime | None
    agency: str | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def detect_agency(url: str, text: str | None) -> str | None:
    host = _host(url)
    for key, (hosts, pattern) in _AGENCIES.items():
        if any(host == h or host.endswith("." + h) for h in hosts):
            return key
        if text and pattern.search(text[:300]):
            return key
    return None


def _prepare(item: ClusterInput) -> _Prepared:
    canonical = canonical_news_url(item.url)
    text = (item.text or "")[:_MAX_TEXT]
    host = (item.domain or _host(canonical)).lower()
    for prefix in _MOBILE_PREFIXES:
        if host.startswith(prefix) and host.count(".") >= 2:
            host = host[len(prefix):]
            break
    return _Prepared(item, canonical, host, simhash(text) if len(text) >= MIN_FULL_BODY_CHARS else None,
                     len(_title_tokens(item.title)) >= 4, _aware(item.published_at) or _aware(item.seen_at),
                     detect_agency(canonical, text))


def _same_story(a: _Prepared, b: _Prepared) -> bool:
    if a.canonical == b.canonical:
        return True
    sim = title_similarity(a.item.title, b.item.title) if a.title_ok and b.title_ok else 0.0
    na, nb = _numbers(a.item.title), _numbers(b.item.title)
    if na and nb and na != nb:
        sim = 0.0   # same wording but different figures is a different story
    if a.sim is not None and b.sim is not None:
        distance = hamming(a.sim, b.sim)
        return distance <= BODY_HAMMING or (sim >= TITLE_SIMILARITY and distance <= BODY_HAMMING_WITH_TITLE)
    if sim < TITLE_SIMILARITY:
        return False
    if a.when and b.when and abs((a.when - b.when).total_seconds()) > TITLE_ONLY_WINDOW_SECONDS:
        return False
    return True


def cluster_items(items: list[ClusterInput]) -> ClusterResult:
    """Cluster ``items``; items beyond ``MAX_ITEMS`` become singletons (reported in ``notes``)."""
    notes: list[str] = []
    prepared = [_prepare(i) for i in items]
    n = min(len(prepared), MAX_ITEMS)
    if len(prepared) > n:
        notes.append(f"Only the first {MAX_ITEMS} items were compared; {len(prepared) - n} were left unclustered.")
    parent = list(range(len(prepared)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_canonical: dict[str, int] = {}
    for i in range(n):
        j = by_canonical.setdefault(prepared[i].canonical, i)
        if j != i:
            parent[find(i)] = find(j)
    for i in range(n):
        for j in range(i + 1, n):
            if find(i) != find(j) and _same_story(prepared[i], prepared[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(prepared)):
        groups.setdefault(find(i), []).append(i)

    far = datetime.max.replace(tzinfo=timezone.utc)
    clusters: list[NewsCluster] = []
    item_cluster: dict[str, str] = {}
    for members in groups.values():
        agencies = sorted({prepared[m].agency for m in members if prepared[m].agency})
        owner = agencies[0] if agencies else None

        def order(m: int) -> tuple:
            p = prepared[m]
            return (p.when or far, 0 if (owner and p.agency == owner and detect_agency(p.canonical, None) == owner) else 1, p.canonical, p.item.id)

        members.sort(key=order)
        rep = prepared[members[0]]
        cluster_id = "cl_" + hashlib.sha256(rep.canonical.encode("utf-8")).hexdigest()[:12]
        def source_key(m: int) -> str:
            p = prepared[m]
            return f"agency:{p.agency}" if p.agency else registrable_domain(p.host) or p.canonical

        keys = {source_key(m) for m in members}
        if len(keys) > 1:   # republishers are not independent of whatever they republish
            keys = {k for k in keys if k.startswith("agency:") or k not in _AGGREGATORS} or keys
        sources = sorted(keys)
        ids = tuple(prepared[m].item.id for m in members)
        clusters.append(NewsCluster(cluster_id, rep.item.id, ids, len(sources), tuple(sources), tuple(agencies)))
        for i in ids:
            item_cluster[i] = cluster_id
    when_of = {p.item.id: p.when for p in prepared}
    clusters.sort(key=lambda c: (when_of.get(c.representative_id) or far, c.cluster_id))
    return ClusterResult(clusters, item_cluster, notes)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed_date = date.fromisoformat(raw[:10])
                return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
            except ValueError:
                return None
        return _aware(parsed)
    return None


def _source_field(source: Any, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
        return None
    for name in names:
        val = getattr(source, name, None)
        if val is not None:
            return val
    return None


def _to_cluster_input(source: Any, index: int) -> tuple[ClusterInput, bool]:
    if isinstance(source, ClusterInput):
        collapsed = " ".join(source.text.split())[:_MAX_TEXT] if isinstance(source.text, str) and source.text.strip() else None
        has_full_body = bool(collapsed and len(collapsed) >= MIN_FULL_BODY_CHARS)
        if collapsed == source.text:
            return source, has_full_body
        return ClusterInput(
            id=source.id,
            url=source.url,
            title=source.title,
            text=collapsed,
            published_at=source.published_at,
            seen_at=source.seen_at,
            domain=source.domain,
        ), has_full_body

    raw_id = _source_field(source, "source_id", "id")
    sid = str(raw_id).strip() if raw_id is not None and str(raw_id).strip() else f"src_{index}"
    raw_url = _source_field(source, "url", "canonical_url")
    url = str(raw_url).strip() if raw_url is not None and str(raw_url).strip() else sid
    raw_title = _source_field(source, "title")
    title = str(raw_title).strip() if raw_title is not None else ""
    raw_text = _source_field(source, "text", "body", "content", "excerpt")
    collapsed = " ".join(str(raw_text).split())[:_MAX_TEXT] if isinstance(raw_text, str) and raw_text.strip() else None
    has_full_body = bool(collapsed and len(collapsed) >= MIN_FULL_BODY_CHARS)
    published_at = _coerce_datetime(_source_field(source, "published_at"))
    seen_at = _coerce_datetime(_source_field(source, "seen_at"))
    raw_domain = _source_field(source, "domain")
    domain = str(raw_domain).strip().lower() if isinstance(raw_domain, str) and raw_domain.strip() else None
    return ClusterInput(
        id=sid,
        url=url,
        title=title,
        text=collapsed,
        published_at=published_at,
        seen_at=seen_at,
        domain=domain,
    ), has_full_body


def recluster_acquired_sources(sources: Iterable[Any]) -> ClusterResult:
    """Re-cluster acquired sources using full-body SimHash (>=200 chars) and wire-agency dateline detection.

    Recalculates ``cluster_id``, ``independent_source_count``, ``independent_sources``, and ``syndicated_from``
    from full fetched article text (fixing pre-acquisition title-only false merges and missed wire syndications).
    """
    items: list[ClusterInput] = []
    short_count = 0
    for idx, src in enumerate(sources):
        item, has_full_body = _to_cluster_input(src, idx)
        items.append(item)
        if not has_full_body:
            short_count += 1
    if not items:
        return ClusterResult(clusters=[], item_cluster={}, notes=[])
    result = cluster_items(items)
    if short_count:
        result.notes.append(
            f"{short_count} acquired source(s) had fewer than {MIN_FULL_BODY_CHARS} characters of body text "
            "and fell back to title/URL clustering."
        )
    return result
