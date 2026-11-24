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


def _fetch(host: str) -> tuple[bytes, str] | None:
    from .openai_compatible import _default_binary_transport
    from .research_fetch import is_host_blocked, validate_public_https_url

    url = f"https://{host}/favicon.ico"
    for _ in range(3):  # The icon itself and up to two redirects, each checked as a public HTTPS URL.
        try:
            validated = validate_public_https_url(url, check_sec_path=False)
        except ValueError:
            return None
        if is_host_blocked(validated):
            return None
        try:
            response = _default_binary_transport("GET", url, {"Accept": "image/*"}, b"", _TIMEOUT_SECONDS,
                                                 max_response_bytes=MAX_ICON_BYTES, connect_timeout_seconds=3.0)
        except Exception:
            return None
        if response.status_code in (301, 302, 303, 307, 308):
            location = {k.lower(): v for k, v in response.headers.items()}.get("location")
            if not location:
                return None
            url = urljoin(url, location)
            continue
        if response.status_code != 200 or not response.body:
            return None
        kind = image_type(response.body)
        return (response.body, kind) if kind else None
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
