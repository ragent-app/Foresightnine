"""Streaming multipart upload of large user documents (PDF / HTML / plain text).

The body is parsed by Starlette's streaming form parser, which spools the file part to disk, so memory stays
bounded by Starlette's spooled form parsing (python-multipart), never by the document size. The same paged pipeline as
fetched documents then builds the page map (PDF text layer, OCR for scanned pages, html_paged for HTML/TXT).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from threading import BoundedSemaphore
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .research_fetch import (
    MAX_FETCH_BYTES, FetchedDocument, FetchFailure, _extract_large_pdf, _retain_original_blob, extract_text_pages,
)
from .research_sources import SourcePermissions

MAX_FIELD_BYTES = 64 * 1024
MAX_FORM_FIELDS = 12
ALLOWED_FIELDS = frozenset({"title", "permissions", "is_simulated", "published_at", "event_at", "revised_at", "text_order"})


class UploadFormError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_form_items(items: list[tuple[str, object]]) -> tuple[dict[str, str], object]:
    """Validate parsed multipart items: known text fields once each, exactly one ``file`` part."""
    fields: dict[str, str] = {}
    upload = None
    for name, value in items:
        if name == "file":
            if upload is not None or isinstance(value, str):
                raise UploadFormError("unexpected_file")
            upload = value
        elif name in ALLOWED_FIELDS and isinstance(value, str) and name not in fields:
            fields[name] = value
        else:
            raise UploadFormError("unexpected_field")
    if upload is None:
        raise UploadFormError("missing_file")
    return fields, upload


@dataclass(frozen=True)
class UploadedFileContext:
    """The two attributes the shared PDF pipeline reads from a request."""
    title: str
    permissions: SourcePermissions


def sniff_upload_kind(path: Path, filename: str | None) -> str:
    """Return 'pdf', 'pptx', 'docx', 'html' or 'text' from content and extension."""
    import zipfile

    with path.open("rb") as handle:
        head = handle.read(8192)
    if not head:
        raise FetchFailure("source_invalid", "The uploaded file is empty.")
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
            if "[Content_Types].xml" in names:
                if "ppt/presentation.xml" in names:
                    return "pptx"
                if "word/document.xml" in names:
                    return "docx"
        except (zipfile.BadZipFile, OSError):
            pass
        raise FetchFailure("source_format_unsupported", "Only PDF, PPTX, DOCX, HTML, Markdown, subtitle or plain text files are supported.")
    if b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise FetchFailure("source_format_unsupported", "Only PDF, PPTX, DOCX, HTML, Markdown, subtitle or plain text files are supported.")
    lowered = (filename or "").lower()
    if lowered.endswith((".html", ".htm", ".xhtml")) or re.search(rb"<\s*(?:!doctype\s+html|html|body|head)\b", head, re.IGNORECASE):
        return "html"
    if lowered.endswith((".txt", ".text", ".md", ".markdown", ".vtt", ".srt")):
        return "text"
    raise FetchFailure("source_format_unsupported", "Only PDF, PPTX, DOCX, HTML, Markdown, subtitle or plain text files are supported.")


@dataclass
class IngestedUpload:
    document: FetchedDocument
    kind: str
    original_base64: str | None


def ingest_uploaded_file(path: Path, *, kind: str, title: str, permissions: SourcePermissions, sha256_hex: str,
                         size: int, ocr_usage=None, text_order: Literal["layout", "reading"] = "layout") -> IngestedUpload:
    """Run the fetched-document pipeline on an uploaded file already on disk.

    PDF: paged text-layer extraction, prioritized local OCR (or vision OCR only when ``ocr_usage`` hooks are bound)
    and blob retention. When a ``DocumentBlobStore`` is configured, original binaries are retained solely in the
    blob store (zero base64 inline in Postgres JSONB); only environments without a blob store keep the <=5MB
    inline base64 fallback for unit tests.
    """
    from .research_blob_store import configured_blob_store

    if not permissions.store:
        raise FetchFailure("source_permission_unknown")
    context = UploadedFileContext(title=title, permissions=permissions)
    blob_configured = configured_blob_store() is not None
    if kind == "pdf":
        document = _extract_large_pdf(path, response_sha256=sha256_hex, final_url="", redirect_count=0,
                                      request=context, ocr_usage=ocr_usage, text_order=text_order)  # type: ignore[arg-type]
        original = None
        if not blob_configured and size <= MAX_FETCH_BYTES:
            original = base64.b64encode(path.read_bytes()).decode("ascii")
            document = replace(document, pages={**(document.pages or {}), "original_retained": True})
        return IngestedUpload(document, kind, original)
    text, pages = extract_text_pages(path, "", is_html=kind == "html")
    original = None
    if blob_configured or size > MAX_FETCH_BYTES:
        pages = _retain_original_blob(pages, path, None)
    else:
        original = base64.b64encode(path.read_bytes()).decode("ascii")
        pages = {**pages, "original_retained": True}
    document = FetchedDocument(text=text, response_sha256=sha256_hex,
                               content_type="text/html" if kind == "html" else "text/plain",
                               final_url="", redirect_count=0, pages=pages)
    return IngestedUpload(document, kind, original)



class HashingFileSink:
    """Binary file writer that counts and hashes bytes while the upload streams in."""

    def __init__(self, path: Path):
        self.path = path
        self._file = path.open("wb")
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> int:
        self._hash.update(data)
        self.size += len(data)
        return self._file.write(data)

    def close(self) -> None:
        self._file.close()

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


class StreamUploadFields(BaseModel):
    """Text fields of the multipart form; ``permissions`` is a JSON object, ``is_simulated`` is true/false."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    permissions: SourcePermissions
    text_order: Literal["layout", "reading"] = "layout"
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None

    @field_validator("published_at", "event_at", "revised_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value


def parse_stream_fields(raw: dict[str, str]) -> StreamUploadFields:
    data: dict = {key: value for key, value in raw.items() if value != ""}
    try:
        if "permissions" in data:
            data["permissions"] = json.loads(data["permissions"])
    except (ValueError, RecursionError) as error:
        raise UploadFormError("invalid_fields") from error
    if "is_simulated" in data:
        flag = data["is_simulated"].strip().lower()
        if flag not in {"true", "false"}:
            raise UploadFormError("invalid_fields")
        data["is_simulated"] = flag == "true"
    try:
        return StreamUploadFields.model_validate(data)
    except ValidationError as error:
        raise UploadFormError("invalid_fields") from error


UPLOAD_WORKERS = BoundedSemaphore(2)
