"""Bounded, local extraction of a user-provided PDF; never runs a shell."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import tempfile
from threading import BoundedSemaphore
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .research_sources import SourcePermissions
from .research_fetch import _TextParser


MAX_PDF_BYTES = 5_000_000
MAX_TEXT_BYTES = 2_000_000
_PDF_WORKERS = BoundedSemaphore(2)


class PdfUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    pdf_base64: str = Field(min_length=1, max_length=4 * ((MAX_PDF_BYTES + 2) // 3))
    text_order: Literal["layout", "reading"] = "layout"
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None
    permissions: SourcePermissions

    @field_validator("published_at", "event_at", "revised_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value


class PdfReextractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text_order: Literal["layout", "reading"]


class HtmlUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=500)
    html_base64: str = Field(min_length=1, max_length=4 * ((MAX_PDF_BYTES + 2) // 3))
    is_simulated: bool = False
    published_at: datetime | None = None
    event_at: datetime | None = None
    revised_at: datetime | None = None
    permissions: SourcePermissions

    @field_validator("published_at", "event_at", "revised_at")
    @classmethod
    def timezone_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return value


class _UploadedHtmlParser(_TextParser):
    """Omit inline XBRL metadata while preserving visible financial tables."""
    def __init__(self) -> None:
        super().__init__()
        self.xbrl_header_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "ix:header":
            self.xbrl_header_depth += 1
        elif not self.xbrl_header_depth:
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self.xbrl_header_depth and tag == "ix:header":
            self.xbrl_header_depth -= 1
        elif not self.xbrl_header_depth:
            super().handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self.xbrl_header_depth:
            super().handle_data(data)


def extract_uploaded_html(request: HtmlUploadRequest) -> tuple[str, str]:
    if not request.permissions.store:
        raise PdfFailure("source_permission_unknown")
    if not _PDF_WORKERS.acquire(blocking=False):
        raise PdfFailure("html_busy")
    try:
        raw = base64.b64decode(request.html_base64, validate=True)
        if not raw or len(raw) > MAX_PDF_BYTES:
            raise ValueError("size")
        html = raw.decode("utf-8-sig")
        parser = _UploadedHtmlParser()
        parser.feed(html)
        parser.close()
        if parser.xbrl_header_depth:
            raise ValueError("unclosed inline XBRL header")
        text = "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())
        if not text or len(text.encode("utf-8")) > MAX_TEXT_BYTES or len(text) > MAX_TEXT_BYTES:
            raise ValueError("text size")
        return text, sha256(raw).hexdigest()
    except (binascii.Error, UnicodeError, ValueError, RecursionError) as error:
        raise PdfFailure("html_invalid_or_too_large") from error
    finally:
        _PDF_WORKERS.release()


class PdfFailure(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExtractedPdf:
    text: str
    original_base64: str
    original_sha256: str


def extract_pdf(request: PdfUploadRequest) -> ExtractedPdf:
    if not request.permissions.store:
        raise PdfFailure("source_permission_unknown")
    try:
        raw = base64.b64decode(request.pdf_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise PdfFailure("invalid_pdf") from error
    if not raw.startswith(b"%PDF-") or len(raw) > MAX_PDF_BYTES:
        raise PdfFailure("invalid_pdf")
    if not _PDF_WORKERS.acquire(blocking=False):
        raise PdfFailure("pdf_busy")
    try:
        text = _extract_text(raw, text_order=request.text_order)
    finally:
        _PDF_WORKERS.release()
    return ExtractedPdf(text=text, original_base64=base64.b64encode(raw).decode("ascii"),
                        original_sha256=sha256(raw).hexdigest())


def _extract_text(raw: bytes, *, text_order: Literal["layout", "reading"] = "layout") -> str:
    if text_order not in {"layout", "reading"}:
        raise PdfFailure("pdf_extraction_mode_invalid")
    extractor = shutil.which("pdftotext")
    limiter = shutil.which("prlimit")
    if not extractor or not limiter:
        raise PdfFailure("pdf_extractor_unavailable")
    with tempfile.TemporaryDirectory(prefix="foresight-pdf-") as directory:
        input_file = Path(directory) / "source.pdf"
        output_file = Path(directory) / "source.txt"
        input_file.write_bytes(raw)
        try:
            result = subprocess.run([limiter, "--as=536870912", "--cpu=10", f"--fsize={MAX_TEXT_BYTES + 1}",
                "--", extractor, "-layout" if text_order == "layout" else "-raw", "-enc", "UTF-8", str(input_file), str(output_file)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, check=False)
        except subprocess.TimeoutExpired as error:
            raise PdfFailure("pdf_timeout") from error
        except OSError as error:
            raise PdfFailure("pdf_extractor_unavailable") from error
        if result.returncode != 0 or not output_file.is_file() or output_file.stat().st_size > MAX_TEXT_BYTES:
            raise PdfFailure("pdf_unreadable_or_too_large")
        try:
            text = output_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise PdfFailure("pdf_unreadable_or_too_large") from error
    if not text.strip():
        raise PdfFailure("pdf_no_extractable_text")
    return text
