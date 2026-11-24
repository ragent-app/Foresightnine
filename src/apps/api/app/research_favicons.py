"""Site icons for the hosts a run reads, fetched by the server so the page can show them.

The browser may only load images from this origin, and a third-party icon service would learn which
sites a user researches. The server asks the public site itself for ``/favicon.ico``, accepts only
small raster images, and remembers each answer (including "none") for a day.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlsplit

HOST = re.compile(r"^(?=.{4,253}$)(?:(?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")
MAX_ICON_BYTES = 64 * 1024
TTL_SECONDS = 24 * 3600
MAX_CACHED = 2000
_TIMEOUT_SECONDS = 4.0

_cache: OrderedDict[str, tuple[float, tuple[bytes, str] | None]] = OrderedDict()
_lock = Lock()


def normalize_host(value: str) -> str | None:
    """A public domain name, lower-case without ``www.``-independent changes; IP literals are refused."""
    if not isinstance(value, str):
        return None
    host = value.strip().lower().rstrip(".")
    return host if HOST.fullmatch(host) else None


def host_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return normalize_host(parts.hostname)


def image_type(data: bytes) -> str | None:
    """Raster formats only: an SVG can carry script, so it is never served."""
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ForesightNine site-icon fetcher)"}
MAX_DOWNLOAD_BYTES = 512 * 1024
MAX_PAGE_BYTES = 1024 * 1024
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_ATTR = re.compile(r"""([a-z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


def _get(url: str, accept: str, limit: int):
    """One GET of a public HTTPS URL; redirects are followed by re-checking each target."""
    from .openai_compatible import _default_binary_transport
    from .research_fetch import is_host_blocked, validate_public_https_url

    for _ in range(3):
        try:
            validated = validate_public_https_url(url, check_sec_path=False)
        except ValueError:
            return None
        if is_host_blocked(validated):
            return None
        try:
            response = _default_binary_transport("GET", url, {**_HEADERS, "Accept": accept}, b"", _TIMEOUT_SECONDS,
                                                 max_response_bytes=limit, connect_timeout_seconds=3.0)
        except Exception:
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = {k.lower(): v for k, v in response.headers.items()}.get("location")
            if not location:
                return None
            url = urljoin(url, location)
            continue
        return (url, response.body) if response.status_code == 200 and response.body else None
    return None


def icon_links(html: str, base_url: str) -> list[str]:
    """Icon URLs a page declares, small icons first and touch icons last; mask icons are skipped."""
    found: list[tuple[int, str]] = []
    for tag in _LINK.findall(html):
        attrs = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR.finditer(tag)}
        rel = attrs.get("rel", "").lower().split()
        href = attrs.get("href", "").strip()
        if not href or "icon" not in " ".join(rel) or "mask-icon" in rel:
            continue
        rank = 2 if "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel else 0
        if rank == 0 and "32x32" not in attrs.get("sizes", "") and attrs.get("sizes"):
            rank = 1
        url = urljoin(base_url, href)
        if url.startswith("https://") and all(url != u for _, u in found):
            found.append((rank, url))
    return [url for _, url in sorted(found, key=lambda item: item[0])]


def smallest_icon(data: bytes) -> tuple[bytes, str] | None:
    """A multi-size .ico is cut down to its image nearest 32 px; other formats pass through as they are."""
    kind = image_type(data)
    if kind != "image/x-icon":
        return (data, kind) if kind and len(data) <= MAX_ICON_BYTES else None
    if len(data) < 6:
        return None
    count = int.from_bytes(data[4:6], "little")
    entries = []
    for i in range(min(count, 64)):
        entry = data[6 + 16 * i: 22 + 16 * i]
        if len(entry) < 16:
            break
        width = entry[0] or 256
        size = int.from_bytes(entry[8:12], "little")
        offset = int.from_bytes(entry[12:16], "little")
        if size <= 0 or offset + size > len(data):
            continue
        entries.append((abs(width - 32), -int.from_bytes(entry[6:8], "little"), entry, data[offset:offset + size]))
    if not entries:
        return None
    _, _, entry, image = min(entries, key=lambda item: item[:2])
    if image.startswith(b"\x89PNG\r\n\x1a\n"):
        return (image, "image/png") if len(image) <= MAX_ICON_BYTES else None
    single = b"\x00\x00\x01\x00\x01\x00" + entry[:12] + (22).to_bytes(4, "little") + image
    return (single, "image/x-icon") if len(single) <= MAX_ICON_BYTES else None


def _fetch(host: str, *, get=_get) -> tuple[bytes, str] | None:
    """/favicon.ico first; failing that, the icons the home page links to."""
    fetched = get(f"https://{host}/favicon.ico", "image/*", MAX_DOWNLOAD_BYTES)
    icon = smallest_icon(fetched[1]) if fetched else None
    if icon:
        return icon
    page = get(f"https://{host}/", "text/html", MAX_PAGE_BYTES)
    if not page:
        return None
    for url in icon_links(page[1][:MAX_PAGE_BYTES].decode("utf-8", "replace"), page[0])[:3]:
        fetched = get(url, "image/*", MAX_DOWNLOAD_BYTES)
        icon = smallest_icon(fetched[1]) if fetched else None
        if icon:
            return icon
    return None


def favicon(host: str, *, fetch=None) -> tuple[bytes, str] | None:
    fetch = fetch or _fetch
    now = monotonic()
    with _lock:
        hit = _cache.get(host)
        if hit is not None and hit[0] > now:
            _cache.move_to_end(host)
            return hit[1]
    result = fetch(host)
    with _lock:
        _cache[host] = (now + TTL_SECONDS, result)
        _cache.move_to_end(host)
        while len(_cache) > MAX_CACHED:
            _cache.popitem(last=False)
    return result
