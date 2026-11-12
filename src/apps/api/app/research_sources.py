"""Account-owned source material with explicit per-use permissions."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .storage import JsonDocumentRepository


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
    kind: Literal["user_supplied", "official"] = "user_supplied"
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


class ResearchSource(BaseModel):
    schema_version: Literal["1"] = "1"
    source_id: str
    owner_id: str
    title: str
    text: str
    url: HttpUrl | None = None
    kind: Literal["user_supplied", "official"]
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None
    obtained_at: datetime
    content_sha256: str
    retrieval_method: Literal["user_supplied", "official_html", "sec_companyconcept", "user_pdf"] = "user_supplied"
    response_sha256: str | None = None
    response_content_type: str | None = None
    original_pdf_base64: str | None = None
    permissions: SourcePermissions


class SourceNotFound(Exception):
    pass


class SourcePermissionError(ValueError):
    pass


def locate_user_quotation(source: ResearchSource, quotation: str) -> dict:
    """Locate an exact user-selected passage without asserting it supports a claim."""
    if not source.permissions.display or not isinstance(quotation, str) or not quotation.strip() or len(quotation) > 1000:
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
            "quotation_match": "exact", "support_status": "not_independently_verified"}


class ResearchSources:
    def __init__(self, repository: JsonDocumentRepository[ResearchSource]):
        self.repository = repository

    def create(self, owner: str, request: SourceInput, *, retrieval_method: Literal["user_supplied", "official_html", "sec_companyconcept", "user_pdf"] = "user_supplied",
               response_sha256: str | None = None, response_content_type: str | None = None,
               original_pdf_base64: str | None = None) -> ResearchSource:
        if not request.permissions.store:
            raise SourcePermissionError("Source storage permission is required.")
        item = ResearchSource(source_id=f"source_{uuid4().hex}", owner_id=owner,
            obtained_at=datetime.now(timezone.utc), content_sha256=sha256(request.text.encode("utf-8")).hexdigest(),
            retrieval_method=retrieval_method, response_sha256=response_sha256, response_content_type=response_content_type,
            original_pdf_base64=original_pdf_base64,
            **request.model_dump())
        self.repository.put(item.source_id, item)
        return item.model_copy(deep=True)

    def get(self, owner: str, source_id: str) -> ResearchSource:
        source = self.repository.get(source_id)
        if source is None or source.owner_id != owner:
            raise SourceNotFound()
        return source.model_copy(deep=True)

    def delete_owner(self, owner: str) -> None:
        for source_id, source in list(self.repository.items()):
            if source.owner_id == owner:
                self.repository.delete(source_id)
