"""Account-owned source material with explicit per-use permissions."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .storage import JsonDocumentRepository, owner_items


class SourcePermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    store: bool = False
    model_input: bool = False
    display: bool = False
    export: bool = False


class SourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=2_000_000)
    url: HttpUrl | None = None
    kind: Literal["user_supplied", "official", "market_data"] = "user_supplied"
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None
    permissions: SourcePermissions

    @field_validator("title")
    @classmethod
    def nonblank_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Title must not be blank.")
        return value

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Source text must not be blank.")
        return value

    @field_validator("url")
    @classmethod
    def url_without_credentials(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and (value.username is not None or value.password is not None):
            raise ValueError("Source URLs cannot include credentials.")
        return value

    @field_validator("published_at", "event_at", "revised_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value


RetrievalMethod = Literal[
    "user_supplied",
    "official_html",
    "sec_companyconcept",
    "user_pdf",
    "user_html",
    "sec_filing_html",
    "historical_prices",
    "web_html",
    "web_pdf",
]


class ResearchSource(BaseModel):
    schema_version: Literal["1"] = "1"
    source_id: str
    owner_id: str
    title: str
    text: str
    url: HttpUrl | None = None
    kind: Literal["user_supplied", "official", "market_data"]
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None
    obtained_at: datetime
    content_sha256: str
    retrieval_method: RetrievalMethod = "user_supplied"
    pdf_text_order: Literal["layout", "reading"] | None = None
    derived_from_source_id: str | None = None
    response_sha256: str | None = None
    response_content_type: str | None = None
    original_pdf_base64: str | None = None
    original_html_base64: str | None = None
    permissions: SourcePermissions
    provenance: dict = Field(default_factory=dict)


class SourceNotFound(Exception):
    pass


class SourcePermissionError(ValueError):
    pass


def table_context_window(text: str, start: int, end: int) -> dict | None:
    """Return bounded preceding rows for a selected pipe-formatted row.

    This is extracted text only: pipe characters and span markers may also
    appear in source content, and neither table identity nor alignment is
    established. Keep exact text and offsets so callers cannot accidentally
    present a reconstructed table as an original quotation.
    """
    if not 0 <= start < end <= len(text) or "\n" in text[start:end]:
        return None
    row_start = text.rfind("\n", 0, start) + 1
    row_end = text.find("\n", end)
    row_end = len(text) if row_end < 0 else row_end

    def row(line: str) -> bool:
        line = line.strip()
        return len(line) <= 4000 and line.startswith("|") and line.endswith("|") and line.count("|") >= 3

    if not row(text[row_start:row_end]):
        return None
    left = row_start
    rows = 1
    while left and rows < 16:
        prior_end = left - 1
        prior_start = text.rfind("\n", 0, prior_end) + 1
        if not row(text[prior_start:prior_end]) or row_end - prior_start > 10_000:
            break
        left = prior_start
        rows += 1
    if left == row_start:
        return None
    prior_end = left - 1
    prior_start = text.rfind("\n", 0, prior_end) + 1 if left else 0
    truncated = bool(left and row(text[prior_start:prior_end]))
    # A short neighbouring heading or unit sentence may explain the rows.
    # It is presented as adjacent text, never as a verified table heading.
    if left and not truncated and prior_end - prior_start <= 500 and row_end - prior_start <= 10_000:
        left = prior_start
    return {"context_start": left, "context_end": row_end, "before": text[left:start],
            "selected": text[start:end], "after": text[end:row_end], "truncated_before": truncated}


def pdf_text_spacing_suspect(source: ResearchSource) -> bool:
    """Conservative signal for heavily joined Latin words, not a quality verdict.

    Sample at most 100,000 characters across the document. Other scripts and
    short excerpts are not assessed; false positives and false negatives remain.
    """
    if source.retrieval_method not in {"user_pdf", "web_pdf"}:
        return False
    text = source.text
    if len(text) <= 100_000:
        sample = text
    else:
        sample = " ".join(text[(len(text) - 20_000) * i // 4: (len(text) - 20_000) * i // 4 + 20_000] for i in range(5))
    words = re.findall(r"[A-Za-z]{2,}", sample)
    total = sum(len(word) for word in words)
    long_words = [word for word in words if len(word) >= 25]
    return total >= 1000 and len(long_words) >= 6 and sum(map(len, long_words)) * 100 >= total * 12


def partial_ascii_token(text: str, start: int, end: int) -> bool:
    """Detect selected fragments inside ASCII words or numeric literals.

    This is a boundary warning, not a semantic support check. Do not apply
    ASCII word assumptions to languages that do not separate words by spaces.
    """
    if not 0 <= start < end <= len(text):
        return True
    def word(char: str) -> bool:
        return char.isascii() and (char.isalnum() or char == "_")
    first, last = text[start], text[end - 1]
    if start and word(first) and word(text[start - 1]):
        return True
    if end < len(text) and word(last) and word(text[end]):
        return True
    if (first.isascii() and first.isdigit() and last.isascii() and last.isdigit() and start and end < len(text)
            and text[start - 1] == "(" and text[end] == ")"):
        return True
    if first.isascii() and first.isdigit() and start and text[start - 1] == "(" and last == ")":
        return True
    if (first == "(" and last.isascii() and last.isdigit() and end < len(text) and text[end] == ")"):
        return True
    if (first in {"-", "−"} and start and start + 1 < end and text[start - 1].isascii()
            and text[start - 1].isdigit() and text[start + 1].isascii() and text[start + 1].isdigit()):
        return True
    if (last in {".", ",", "/"} and end < len(text) and text[end].isascii() and text[end].isdigit()
            and end - start >= 2 and text[end - 2].isascii() and text[end - 2].isdigit()):
        return True
    if first.isascii() and first.isdigit() and start:
        previous = text[start - 1]
        if previous in {"-", "−"}:
            return True
        if previous in {".", ",", "/"} and start >= 2 and text[start - 2].isascii() and text[start - 2].isdigit():
            return True
    if (last.isascii() and last.isdigit() and end + 1 < len(text) and text[end] in {".", ",", "/", "-"}
            and text[end + 1].isascii() and text[end + 1].isdigit()):
        return True
    return False


def locate_user_quotation(source: ResearchSource, quotation: str) -> dict:
    """Locate an exact user-selected passage without asserting it supports a claim."""
    if source.is_simulated or not source.permissions.display or not isinstance(quotation, str) or not quotation.strip() or len(quotation) > 1000:
        raise ValueError("Quotation is unavailable.")
    matches: list[dict] = []
    offset = 0
    while len(matches) < 6:
        start = source.text.find(quotation, offset)
        if start < 0:
            break
        position = {"character_start": start, "character_end": start + len(quotation)}
        if source.retrieval_method == "user_pdf":
            position["page"] = source.text[:start].count("\f") + 1
        matches.append(position)
        offset = start + 1  # Include overlapping matches, so uniqueness is not overstated.
    if not matches:
        raise ValueError("Quotation is unavailable.")
    location = matches[0] if len(matches) == 1 else {"status": "ambiguous", "matches": matches[:5], "truncated": len(matches) > 5}
    return {"source_id": source.source_id, "quotation": quotation, "location": location,
            "quotation_match": "exact", "support_status": "not_independently_verified",
            **({"partial_token": True} if any(partial_ascii_token(source.text, pos["character_start"], pos["character_end"]) for pos in matches) else {})}


class ResearchSources:
    def __init__(self, repository: JsonDocumentRepository[ResearchSource]):
        self.repository = repository

    def create(self, owner: str, request: SourceInput, *, retrieval_method: RetrievalMethod = "user_supplied",
               response_sha256: str | None = None, response_content_type: str | None = None,
               original_pdf_base64: str | None = None, original_html_base64: str | None = None, pdf_text_order: Literal["layout", "reading"] | None = None,
               derived_from_source_id: str | None = None, source_id: str | None = None,
               provenance: dict | None = None) -> ResearchSource:
        if not request.permissions.store:
            raise SourcePermissionError("Source storage permission is required.")
        item = ResearchSource(source_id=source_id or f"source_{uuid4().hex}", owner_id=owner,
            obtained_at=datetime.now(timezone.utc), content_sha256=sha256(request.text.encode("utf-8")).hexdigest(),
            retrieval_method=retrieval_method, response_sha256=response_sha256, response_content_type=response_content_type,
            original_pdf_base64=original_pdf_base64, original_html_base64=original_html_base64, pdf_text_order=pdf_text_order,
            derived_from_source_id=derived_from_source_id, provenance=provenance or {},
            **request.model_dump())
        self.repository.put(item.source_id, item)
        return item.model_copy(deep=True)

    def get(self, owner: str, source_id: str) -> ResearchSource:
        source = self.repository.get(source_id)
        if source is None or source.owner_id != owner:
            raise SourceNotFound()
        return source.model_copy(deep=True)

    def delete_owner(self, owner: str) -> None:
        for source_id, source in list(owner_items(self.repository, owner)):
            if source.owner_id == owner:
                self.repository.delete(source_id)
