"""Bounded retrieval of selected official HTML, with no redirects or cookies."""
from __future__ import annotations

from html.parser import HTMLParser
from dataclasses import dataclass
from hashlib import sha256
from datetime import datetime
import re
from typing import Callable
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .openai_compatible import HttpResponse, _TransportFailure, _TransportTimeout, _default_transport
from .research_sources import SourcePermissions


ALLOWED_HOSTS = frozenset({"www.bea.gov", "www.federalreserve.gov"})
HTML_EXTRACTION_DISCLOSURE = ("Webpage text is extracted; table separators and span markers, when present, "
                              "are generated. Check the original webpage for layout.")


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
        parts = urlsplit(str(value))
        if (parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or
                parts.username is not None or parts.password is not None or
                parts.port not in (None, 443) or parts.fragment):
            raise ValueError("Only supported official HTTPS sources are available.")
        return value


class FetchFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []
        self.cell_depth = 0
        self.row_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag in {"td", "th"}:
            self.cell_depth += 1
            self.parts.append(" | ")
            # These are extraction annotations, not source quotations. Keep
            # only bounded numeric spans; do not copy arbitrary attributes.
            for name, value in attrs:
                if (name in {"colspan", "rowspan"} and value and len(value) <= 2 and value.isascii() and
                        value.isdigit() and 1 < int(value) <= 99):
                    self.parts.append(f"[{name}={int(value)}] ")
        elif tag == "tr":
            self.row_depth += 1
            self.parts.append("\n")
        elif tag in {"p", "div", "br", "h1", "h2", "h3", "li"}:
            self.parts.append(" " if self.cell_depth else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"} and self.hidden:
            self.hidden -= 1
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.cell_depth:
            self.cell_depth -= 1
        elif tag == "tr":
            self.parts.append(" |\n")
            self.row_depth = max(0, self.row_depth - 1)
        elif tag in {"p", "div", "h1", "h2", "h3", "li"}:
            self.parts.append(" " if self.cell_depth else "\n")

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.row_depth and not self.cell_depth and not data.strip():
            return
        self.parts.append(re.sub(r"\s+", " ", data) if self.cell_depth else data)


@dataclass(frozen=True)
class FetchedText:
    text: str
    response_sha256: str
    content_type: str


def fetch_official_text(request: FetchSourceRequest,
                        transport: Callable[..., HttpResponse] = _default_transport) -> FetchedText:
    if not request.permissions.store:
        raise FetchFailure("source_permission_unknown")
    try:
        response = transport("GET", str(request.url), {"Accept": "text/html, text/plain",
            "User-Agent": "ForesightNine-Research/1.0"}, b"", 15.0)
    except _TransportTimeout as error:
        raise FetchFailure("source_timeout") from error
    except (_TransportFailure, OSError) as error:
        raise FetchFailure("source_unavailable") from error
    if response.status_code == 429:
        raise FetchFailure("source_rate_limited")
    if not 200 <= response.status_code < 300:
        # Redirects are deliberately not followed, including to other hosts.
        raise FetchFailure("source_unavailable")
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
    if content_type not in {"text/html", "text/plain"}:
        raise FetchFailure("source_format_unsupported")
    if content_type == "text/plain":
        text = response.body.strip()
    else:
        parser = _TextParser()
        try:
            parser.feed(response.body)
            parser.close()
        except (ValueError, RecursionError) as error:
            raise FetchFailure("source_invalid") from error
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
    if not text or len(text) > 200000:
        raise FetchFailure("source_empty_or_too_large")
    return FetchedText(text=text, response_sha256=sha256(response.body.encode("utf-8")).hexdigest(),
                       content_type=content_type)
