"""Bounded, optional PDF geometry mapped to immutable extracted text.

Geometry is a reading aid, not a verification of the PDF's visual meaning.
All locations refer to the stored text; uncertain alignments are never used.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import BoundedSemaphore
from typing import Callable
import xml.etree.ElementTree as ET

from .research_sources import ResearchSource
from .research_uploads import MAX_PDF_BYTES

GEOMETRY_METHOD = 'poppler_bbox_layout_exact_v1'
_MAX_XML = 8_000_000
_MAX_WORDS = 100_000
_MAX_GROUP_LINES = 1_200
_WORKERS = BoundedSemaphore(2)


@dataclass(frozen=True)
class GeometryLine:
    page: int
    start: int
    end: int
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class GeometryTableRow:
    page: int
    start: int
    end: int
    cells: tuple[tuple[str, float, float], ...]
    bounds: tuple[float, float, float, float]


@dataclass(frozen=True)
class GeometryEvidenceGroup:
    kind: str
    parts: tuple[tuple[int, int], ...]
    page: int
    bounds: tuple[float, float, float, float]
    dimension_status: str = 'not_independently_verified'


@dataclass(frozen=True)
class PdfGeometry:
    method: str
    pdf_sha256: str
    text_sha256: str
    lines: tuple[GeometryLine, ...]
    unmatched_lines: int
    table_rows: tuple[GeometryTableRow, ...] = ()


def geometry_groups_truncated(geometry: PdfGeometry) -> bool:
    return len(geometry.lines) + len(geometry.table_rows) > _MAX_GROUP_LINES


def pdf_structure_diagnostic(source_id: str, geometry: PdfGeometry | None,
                             groups: list[GeometryEvidenceGroup]) -> dict:
    """Report only actual local mapping work, never whole-document support."""
    return {'source_id': source_id,
            'geometry_status': ('unavailable' if geometry is None else
                                'limit_reached' if geometry_groups_truncated(geometry) else 'mapped'),
            'mapped_line_count': len(geometry.lines) if geometry is not None else None,
            'unmatched_line_count': geometry.unmatched_lines if geometry is not None else None,
            'group_count': len(groups) if geometry is not None and not geometry_groups_truncated(geometry) else None,
            'reading_order_status': 'not_independently_verified'}


def complete_sentence_line(text: str, line: GeometryLine) -> bool:
    """Admit only a whole extracted paragraph that parses as one sentence."""
    from .research_context import evidence_units
    if not 0 <= line.start < line.end <= len(text):
        return False
    prefix = text[max(0, line.start - 512):line.start]
    if line.start and not re.search(r'(?:\f|\n[ \t]*\n)[ \t]*$', prefix):
        return False
    fragment = text[line.start:line.end]
    units = evidence_units(fragment)
    return any(unit.kind == 'sentence' and unit.parts == ((0, fragment),) for unit in units)


def map_geometry(xml: bytes, text: str, pdf_sha256: str) -> PdfGeometry:
    """Map unique complete lines on their own page; ambiguous lines stay unknown."""
    if len(xml) > _MAX_XML or b'<!ENTITY' in xml.upper():
        raise ValueError('unsafe geometry')
    root = ET.fromstring(xml)
    pages = text.split('\f')
    starts = []
    cursor = 0
    for page in pages:
        starts.append(cursor)
        cursor += len(page) + 1
    result: list[GeometryLine] = []
    table_rows: list[GeometryTableRow] = []
    unmatched = 0
    count = 0
    for page_index, page in enumerate(root.findall('.//{*}page')):
        if page_index >= len(pages):
            break
        for line in page.findall('.//{*}line'):
            words = line.findall('.//{*}word')
            count += len(words)
            if count > _MAX_WORDS:
                raise ValueError('too many words')
            tokens = [''.join(word.itertext()) for word in words]
            if not tokens or len(tokens) > 200 or any(not token or len(token) > 1000 for token in tokens):
                unmatched += 1
                continue
            # A line with a wide geometric gap may span columns or table cells.
            # Do not turn it into a continuous evidence unit.
            try:
                boxes = [tuple(float(w.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax')) for w in words]
                bounds = tuple(float(line.attrib[k]) for k in ('xMin', 'yMin', 'xMax', 'yMax'))
            except (KeyError, ValueError):
                unmatched += 1
                continue
            if not all(math.isfinite(value) for box in [*boxes, bounds] for value in box):
                unmatched += 1
                continue
            if any(b[0] < a[0] for a, b in zip(boxes, boxes[1:])):
                unmatched += 1
                continue
            pattern = r'(?<!\S)' + r'[ \t]+'.join(re.escape(token) for token in tokens) + r'(?!\S)'
            matches = re.finditer(pattern, pages[page_index])
            match = next(matches, None)
            if match is None or next(matches, None) is not None:
                unmatched += 1
                continue
            gaps = [i for i, (a, b) in enumerate(zip(boxes, boxes[1:])) if b[0] - a[2] > 36]
            if gaps:
                cells = []
                edges = [-1, *gaps, len(tokens) - 1]
                for left, right in zip(edges, edges[1:]):
                    begin, end = left + 1, right + 1
                    cells.append((' '.join(tokens[begin:end]), boxes[begin][0], boxes[end - 1][2]))
                table_rows.append(GeometryTableRow(page_index + 1, starts[page_index] + match.start(),
                                                  starts[page_index] + match.end(), tuple(cells), bounds))
                unmatched += 1  # A row alone is not a validated table group.
                continue
            result.append(GeometryLine(page_index + 1, starts[page_index] + match.start(),
                                       starts[page_index] + match.end(), bounds))
    return PdfGeometry(GEOMETRY_METHOD, pdf_sha256, sha256(text.encode('utf-8')).hexdigest(), tuple(result), unmatched, tuple(table_rows))


def geometry_evidence_groups(text: str, geometry: PdfGeometry) -> list[GeometryEvidenceGroup]:
    """Return only exact, conservatively grouped original-text ranges.

    Geometry establishes a plausible layout relationship, not semantic support
    or an independent verification of the original document's reading order.
    """
    if sha256(text.encode('utf-8')).hexdigest() != geometry.text_sha256:
        return []
    if geometry_groups_truncated(geometry):
        return []  # Keep geometry work bounded; the omitted layout remains unknown.
    from .research_context import evidence_units
    groups: list[GeometryEvidenceGroup] = []
    lines = sorted(geometry.lines, key=lambda item: (item.page, item.start))
    for index, first in enumerate(lines):
        prefix = text[max(0, first.start - 512):first.start]
        if first.start and not re.search(r'(?:\f|\n[ \t]*\n)[ \t]*$', prefix):
            continue
        if complete_sentence_line(text, first):
            groups.append(GeometryEvidenceGroup('sentence', ((first.start, first.end),), first.page, first.bounds))
            continue
        chosen = [first]
        for next_line in lines[index + 1:index + 8]:
            previous = chosen[-1]
            if next_line.page != first.page or not re.fullmatch(r'[ \t]*\n[ \t]*', text[previous.end:next_line.start]):
                break
            height = max(previous.bounds[3] - previous.bounds[1], next_line.bounds[3] - next_line.bounds[1], 1)
            if (abs(next_line.bounds[0] - first.bounds[0]) > 12 or
                    next_line.bounds[1] < previous.bounds[3] or
                    next_line.bounds[1] - previous.bounds[3] > 1.5 * height):
                break
            # Any competing line at either vertical band makes reading order
            # ambiguous, including a separate column on the same page.
            if any(other.page == first.page and other not in (previous, next_line) and
                   other.bounds[1] < next_line.bounds[3] and other.bounds[3] > previous.bounds[1]
                   for other in lines):
                break
            chosen.append(next_line)
            fragment = text[first.start:next_line.end]
            if len(fragment) > 3000:
                break
            if re.search(r'[.!?。！？][\"\'”’)]*$', fragment):
                units = evidence_units(fragment)
                if any(unit.kind == 'sentence' and unit.parts == ((0, fragment),) for unit in units):
                    groups.append(GeometryEvidenceGroup('sentence', ((first.start, next_line.end),), first.page,
                        (min(item.bounds[0] for item in chosen), first.bounds[1],
                         max(item.bounds[2] for item in chosen), next_line.bounds[3])))
                break
    rows = sorted(geometry.table_rows, key=lambda item: (item.page, item.start))
    number = re.compile(r'^[\s\d.,%+−–()$€£-]+$')
    for header, row in zip(rows, rows[1:]):
        if (header.page != row.page or not 2 <= len(header.cells) <= 8 or len(header.cells) != len(row.cells)
                or not re.fullmatch(r'[ \t]*\n[ \t]*', text[header.end:row.start])):
            continue
        height = max(header.bounds[3] - header.bounds[1], row.bounds[3] - row.bounds[1], 1)
        if row.bounds[1] < header.bounds[3] or row.bounds[1] - header.bounds[3] > 1.5 * height:
            continue
        if any(abs(a[1] - b[1]) > 12 for a, b in zip(header.cells, row.cells)):
            continue
        if not any(re.search(r'[A-Za-z\u3400-\u9fff]|(?:19|20)\d{2}', cell[0]) for cell in header.cells[1:]):
            continue
        if not re.search(r'[A-Za-z\u3400-\u9fff]', row.cells[0][0]) or not all(number.fullmatch(cell[0]) for cell in row.cells[1:]):
            continue
        if any(line.page == header.page and line.bounds[1] < row.bounds[3] and line.bounds[3] > header.bounds[1]
               for line in lines):
            continue
        groups.append(GeometryEvidenceGroup('table', ((header.start, header.end), (row.start, row.end)), header.page,
            (min(header.bounds[0], row.bounds[0]), header.bounds[1], max(header.bounds[2], row.bounds[2]), row.bounds[3])))
    return groups


def extract_geometry(source: ResearchSource, should_stop: Callable[[], bool] | None = None) -> PdfGeometry | None:
    """Extract an ephemeral hash-bound sidecar, without changing source text."""
    if (source.retrieval_method != 'user_pdf' or not source.permissions.store or
            not source.permissions.model_input or not source.original_pdf_base64 or not source.response_sha256):
        return None
    try:
        raw = base64.b64decode(source.original_pdf_base64, validate=True)
    except (ValueError, binascii.Error):
        return None
    digest = sha256(raw).hexdigest()
    if (len(raw) > MAX_PDF_BYTES or not raw.startswith(b'%PDF-') or digest != source.response_sha256 or
            sha256(source.text.encode('utf-8')).hexdigest() != source.content_sha256):
        return None
    executable, limiter = shutil.which('pdftotext'), shutil.which('prlimit')
    if not executable or not limiter or not _WORKERS.acquire(blocking=False):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='foresight-pdf-geometry-') as directory:
            path, output = Path(directory) / 'source.pdf', Path(directory) / 'geometry.xml'
            path.write_bytes(raw)
            try:
                process = subprocess.Popen([limiter, '--as=536870912', '--cpu=3', f'--fsize={_MAX_XML + 1}',
                    '--', executable, '-bbox-layout', '-enc', 'UTF-8', str(path), str(output)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                import time
                deadline = time.monotonic() + 4
                try:
                    while process.poll() is None:
                        if (should_stop and should_stop()) or time.monotonic() >= deadline:
                            process.kill()
                            process.wait()
                            return None
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                if process.returncode or not output.is_file() or output.stat().st_size > _MAX_XML:
                    return None
                return map_geometry(output.read_bytes(), source.text, digest)
            except (OSError, subprocess.TimeoutExpired, ET.ParseError, ValueError, RecursionError):
                return None
    finally:
        _WORKERS.release()
