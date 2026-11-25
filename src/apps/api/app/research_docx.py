"""Pure-stdlib OOXML Word (.docx) parser with Zip-Bomb/XXE guards and clean track-changes handling.

Reads ``word/document.xml`` paragraphs and tables (emitted as Markdown tables), explicitly
skipping ``<w:del>`` tracked deletions while preserving normal text and ``<w:ins>`` tracked
insertions. Produces ``html_paged`` pseudo-pages compatible with ``document_pages``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from .research_documents import DEFAULT_MAX_TEXT_CHARS, TextPager
from .research_memory_gate import heavy_memory_section
from .research_pptx import (
    MAX_OOXML_MEMBERS,
    MAX_PPTX_UPLOAD_BYTES,
    OoxmlSecurityError,
    _BudgetTracker,
    _local,
    read_zip_member_capped,
    safe_parse_xml,
)

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_HEAVY_GATE_THRESHOLD_BYTES = 5 * 1024 * 1024


def _collect_paragraph_text(p_elem: ET.Element) -> str:
    """Collect visible text from ``<w:p>``, explicitly skipping ``<w:del>`` tracked deletions."""
    parts: list[str] = []

    def _walk(node: ET.Element) -> None:
        lname = _local(node.tag)
        if lname in ("del", "delText", "moveFrom", "instrText"):
            return
        if lname == "t" and node.text:
            parts.append(node.text)
            return
        if lname == "tab":
            parts.append(" ")
            return
        if lname in ("br", "cr"):
            parts.append("\n")
            return
        for child in node:
            _walk(child)

    _walk(p_elem)
    return re.sub(r"[ \t]+", " ", "".join(parts)).strip()


def _extract_word_table(tbl: ET.Element) -> list[str]:
    rows: list[list[str]] = []
    for tr in tbl.findall(f"./{{{_W_NS}}}tr"):
        cells: list[str] = []
        for tc in tr.findall(f"./{{{_W_NS}}}tc"):
            p_texts = [_collect_paragraph_text(p) for p in tc.findall(f"./{{{_W_NS}}}p")]
            cell = " ".join(t for t in p_texts if t).replace("|", "/")
            cells.append(cell)
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return []
    ncols = max(len(r) for r in rows)
    md: list[str] = []
    for idx, row in enumerate(rows):
        padded = row + [""] * (ncols - len(row))
        md.append("| " + " | ".join(padded[:ncols]) + " |")
        if idx == 0:
            md.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
    return md


@dataclass(frozen=True)
class DocxExtractionResult:
    text: str
    document_pages: dict[str, Any]
    paragraph_count: int
    table_count: int


def extract_docx(path: Path) -> DocxExtractionResult:
    """Extract clean paragraphs and Markdown tables from a ``.docx`` file."""
    file_size = path.stat().st_size
    if file_size > MAX_PPTX_UPLOAD_BYTES:
        raise OoxmlSecurityError("docx_too_large", f"DOCX exceeds {MAX_PPTX_UPLOAD_BYTES} byte upload cap.")

    def _run() -> DocxExtractionResult:
        budget = _BudgetTracker()
        try:
            zf = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, OSError) as err:
            raise OoxmlSecurityError("docx_invalid_archive", f"Invalid DOCX archive: {err}") from err

        with zf:
            names = zf.namelist()
            if len(names) > MAX_OOXML_MEMBERS:
                raise OoxmlSecurityError("ooxml_too_many_members", f"DOCX contains {len(names)} members.")
            if "word/document.xml" not in names:
                raise OoxmlSecurityError("docx_missing_document", "DOCX is missing word/document.xml.")
            raw = read_zip_member_capped(zf, "word/document.xml", budget)
            root = safe_parse_xml(raw)
            body = root.find(f"./{{{_W_NS}}}body")
            if body is None:
                body = root

            pager = TextPager(max_text_chars=DEFAULT_MAX_TEXT_CHARS)
            para_count = 0
            table_count = 0
            for child in body:
                lname = _local(child.tag)
                if lname == "p":
                    txt = _collect_paragraph_text(child)
                    if txt:
                        para_count += 1
                        for sub in txt.splitlines():
                            if sub.strip():
                                pager.add_line(sub.strip())
                elif lname == "tbl":
                    md_rows = _extract_word_table(child)
                    if md_rows:
                        table_count += 1
                        for row_line in md_rows:
                            pager.add_line(row_line)
            root.clear()
            text, pages = pager.finish(original_retained=False)
            pages = {**pages, "method": "docx_paged_v1"}
            return DocxExtractionResult(
                text=text,
                document_pages=pages,
                paragraph_count=para_count,
                table_count=table_count,
            )

    if file_size > _HEAVY_GATE_THRESHOLD_BYTES:
        with heavy_memory_section():
            return _run()
    return _run()
