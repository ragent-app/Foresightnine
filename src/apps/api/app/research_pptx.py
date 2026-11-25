"""Pure-stdlib OOXML PowerPoint (.pptx) parser with production Zip-Bomb/XXE guards.

Follows the OOXML relationship graph strictly (``ppt/presentation.xml`` ->
``ppt/_rels/presentation.xml.rels`` -> per-slide ``ppt/slides/_rels/slideX.xml.rels``) so
reordered/deleted slides never mismatch Speaker Notes, Charts, or SmartArt. Extracts chart
values with both ``<c:numFmt>`` percentage scaling and ``<c:dispUnits>`` (thousands/millions/
billions) unit awareness, flags hidden slides (``show="0"``), expands table ``gridSpan``/
``rowSpan``, and emits ``\\f``-paginated text compatible with ``document_pages``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import io
import posixpath
import re
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from .research_documents import PAGES_FORMAT_VERSION
from .research_memory_gate import heavy_memory_section

MAX_PPTX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_OOXML_MEMBERS = 500
MAX_OOXML_SLIDES = 300
MAX_OOXML_MEMBER_UNCOMPRESSED = 4 * 1024 * 1024
MAX_OOXML_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024
MAX_OOXML_COMPRESSION_RATIO = 50.0
MAX_OOXML_GROUP_DEPTH = 16
MAX_OOXML_XML_NODES = 200_000
_HEAVY_GATE_THRESHOLD_BYTES = 5 * 1024 * 1024

_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

_DISP_UNIT_SUFFIX: dict[str, str] = {
    "hundreds": "hundreds",
    "thousands": "thousands",
    "tenThousands": "10-thousands",
    "hundredThousands": "100-thousands",
    "millions": "millions",
    "tenMillions": "10-millions",
    "hundredMillions": "100-millions",
    "billions": "billions",
    "trillions": "trillions",
}


class OoxmlSecurityError(ValueError):
    """Raised when an OOXML archive violates Zip-Bomb, XXE, or path-traversal bounds."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _BudgetTracker:
    total_read: int = 0
    max_total: int = MAX_OOXML_TOTAL_UNCOMPRESSED


def normalize_ooxml_target(base_dir: str, target: str, allowed_prefixes: tuple[str, ...]) -> str | None:
    """Resolve a ``.rels`` Target relative to ``base_dir`` and enforce prefix whitelist without ``..``."""
    raw = (target or "").strip().replace("\\", "/")
    if not raw or raw.startswith(("http://", "https://", "mailto:", "file:")):
        return None
    if raw.startswith("/"):
        norm = posixpath.normpath(raw.lstrip("/"))
    else:
        norm = posixpath.normpath(posixpath.join(base_dir, raw))
    if norm.startswith("..") or "/../" in norm or norm.startswith("/"):
        raise OoxmlSecurityError("ooxml_path_traversal", f"Disallowed OOXML target path: {target!r}")
    if not any(norm.startswith(pref) for pref in allowed_prefixes):
        return None
    return norm


def read_zip_member_capped(
    zf: zipfile.ZipFile,
    name: str,
    budget: _BudgetTracker,
    *,
    max_bytes: int = MAX_OOXML_MEMBER_UNCOMPRESSED,
) -> bytes:
    """Stream-read a ZIP member with hard caps on actual decompressed bytes (never trusting ZipInfo.file_size)."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return b""
    chunks: list[bytes] = []
    member_read = 0
    with zf.open(info, "r") as stream:
        while True:
            blk = stream.read(65_536)
            if not blk:
                break
            member_read += len(blk)
            budget.total_read += len(blk)
            if member_read > max_bytes:
                raise OoxmlSecurityError("ooxml_member_too_large", f"OOXML member {name} exceeded {max_bytes} bytes.")
            if budget.total_read > budget.max_total:
                raise OoxmlSecurityError("ooxml_zip_bomb", f"OOXML decompressed total exceeded {budget.max_total} bytes.")
            chunks.append(blk)
    compressed = max(1, int(info.compress_size or 0))
    if member_read > 65_536 and (member_read / float(compressed)) > MAX_OOXML_COMPRESSION_RATIO:
        raise OoxmlSecurityError("ooxml_zip_bomb", f"OOXML member {name} exceeded compression ratio limit.")
    raw = b"".join(chunks)
    # Pre-parse XXE / DTD bomb scan
    head_upper = raw[:8192].upper()
    if b"<!DOCTYPE" in head_upper or b"<!ENTITY" in head_upper:
        raise OoxmlSecurityError("ooxml_xxe_rejected", f"XML DOCTYPE/ENTITY disallowed in {name}.")
    return raw


def safe_parse_xml(raw: bytes, *, max_nodes: int = MAX_OOXML_XML_NODES) -> ET.Element:
    """Parse XML bytes with node-count bounding to prevent billion-laughs/DOM-bloat attacks."""
    if not raw:
        raise OoxmlSecurityError("ooxml_empty_xml", "Empty XML payload.")
    node_count = 0
    root: ET.Element | None = None
    try:
        for event, elem in ET.iterparse(io.BytesIO(raw), events=("start", "end")):
            if event == "start":
                if root is None:
                    root = elem
                node_count += 1
                if node_count > max_nodes:
                    raise OoxmlSecurityError("ooxml_xml_too_complex", f"XML exceeded {max_nodes} nodes.")
    except ET.ParseError as err:
        raise OoxmlSecurityError("ooxml_invalid_xml", f"Malformed OOXML XML: {err}") from err
    if root is None:
        raise OoxmlSecurityError("ooxml_empty_xml", "XML had no root element.")
    return root


def _parse_rels(zf: zipfile.ZipFile, rels_path: str, base_dir: str, budget: _BudgetTracker,
                allowed_prefixes: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """Return ``{rId: (rel_type, normalized_target)}`` for a ``.rels`` file."""
    if rels_path not in zf.namelist():
        return {}
    raw = read_zip_member_capped(zf, rels_path, budget)
    if not raw:
        return {}
    root = safe_parse_xml(raw)
    out: dict[str, tuple[str, str]] = {}
    for rel in root:
        if not rel.tag.endswith("Relationship"):
            continue
        rid = rel.attrib.get("Id", "").strip()
        rtype = rel.attrib.get("Type", "").strip()
        target = rel.attrib.get("Target", "").strip()
        tmode = rel.attrib.get("TargetMode", "").strip().lower()
        if not rid or tmode == "external":
            continue
        norm = normalize_ooxml_target(base_dir, target, allowed_prefixes)
        if norm:
            out[rid] = (rtype, norm)
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _extract_txbody_lines(elem: ET.Element) -> list[str]:
    lines: list[str] = []
    for p in elem.iter(f"{{{_NS['a']}}}p"):
        runs = [t.text for t in p.iter(f"{{{_NS['a']}}}t") if t.text]
        joined = "".join(runs).strip()
        if joined:
            lines.append(re.sub(r"\s+", " ", joined))
    return lines


def _extract_table_markdown(tbl: ET.Element) -> list[str]:
    """Extract an OOXML ``<a:tbl>`` preserving ``gridSpan`` and ``rowSpan`` merged cells."""
    raw_rows: list[list[tuple[str, int, int, bool, bool]]] = []
    for tr in tbl.findall(f"{{{_NS['a']}}}tr"):
        row_cells: list[tuple[str, int, int, bool, bool]] = []
        for tc in tr.findall(f"{{{_NS['a']}}}tc"):
            txt = " ".join(_extract_txbody_lines(tc)).replace("|", "/")
            grid_span = max(1, int(tc.attrib.get("gridSpan", "1") or "1"))
            row_span = max(1, int(tc.attrib.get("rowSpan", "1") or "1"))
            h_merge = tc.attrib.get("hMerge") in ("1", "true")
            v_merge = tc.attrib.get("vMerge") in ("1", "true")
            row_cells.append((txt, grid_span, row_span, h_merge, v_merge))
        if row_cells:
            raw_rows.append(row_cells)
    if not raw_rows:
        return []

    ncols = max(len(r) for r in raw_rows)
    grid: list[list[str]] = [["" for _ in range(ncols)] for _ in range(len(raw_rows))]
    for r_idx, row_cells in enumerate(raw_rows):
        for c_idx, (txt, _gs, _rs, h_merge, v_merge) in enumerate(row_cells):
            if c_idx >= ncols:
                break
            if v_merge and r_idx > 0 and not txt:
                grid[r_idx][c_idx] = grid[r_idx - 1][c_idx]
            elif h_merge and c_idx > 0 and not txt:
                grid[r_idx][c_idx] = grid[r_idx][c_idx - 1]
            else:
                grid[r_idx][c_idx] = txt

    md: list[str] = []
    for idx, row in enumerate(grid):
        if any(cell.strip() for cell in row):
            md.append("| " + " | ".join(cell.strip() for cell in row) + " |")
            if idx == 0:
                md.append("| " + " | ".join("---" for _ in row) + " |")
    return md


def _format_chart_number(val_str: str, fmt_code: str | None) -> str:
    raw = (val_str or "").strip()
    if not raw:
        return ""
    try:
        dec = Decimal(raw)
    except InvalidOperation:
        return raw
    if fmt_code and "%" in fmt_code:
        pct = dec * Decimal("100")
        # Determine decimal places from format code (e.g. "0.0%" -> 1, "0.00%" -> 2)
        m_dp = re.search(r"\.(0+)", fmt_code)
        dp = len(m_dp.group(1)) if m_dp else 1
        q = Decimal("1").scaleb(-dp)
        return f"{pct.quantize(q)}%"
    if dec == dec.to_integral_value():
        return f"{int(dec):,}"
    s = f"{dec.quantize(Decimal('0.01'))}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _parse_chart_xml(raw: bytes, slide_num: int) -> list[str]:
    """Parse ``ppt/charts/chart*.xml`` extracting title, ``dispUnits``, categories, and formatted series."""
    root = safe_parse_xml(raw)
    title_parts = [t.text.strip() for t in root.findall(f".//{{{_NS['c']}}}title//{{{_NS['a']}}}t") if t.text and t.text.strip()]
    chart_title = " ".join(title_parts) or "Chart"

    # Extract value axis display units (thousands / millions / billions)
    disp_unit: str | None = None
    for bu in root.findall(f".//{{{_NS['c']}}}valAx//{{{_NS['c']}}}dispUnits/{{{_NS['c']}}}builtInUnit"):
        val = bu.attrib.get("val", "").strip()
        if val in _DISP_UNIT_SUFFIX:
            disp_unit = _DISP_UNIT_SUFFIX[val]
            break
    if disp_unit is None:
        custom_lbl = [
            t.text.strip()
            for t in root.findall(f".//{{{_NS['c']}}}valAx//{{{_NS['c']}}}dispUnits//{{{_NS['a']}}}t")
            if t.text and t.text.strip()
        ]
        if custom_lbl:
            disp_unit = " ".join(custom_lbl)

    header = f"[Slide {slide_num} Chart: {chart_title}" + (f" (Units: {disp_unit})]" if disp_unit else "]")

    series_rows: list[tuple[str, list[str], list[str]]] = []
    categories_master: list[str] = []
    for ser in root.findall(f".//{{{_NS['c']}}}ser"):
        # Series name
        s_name_parts = [v.text.strip() for v in ser.findall(f"./{{{_NS['c']}}}tx//{{{_NS['c']}}}v") if v.text and v.text.strip()]
        s_name = " ".join(s_name_parts) or "Series"

        # Categories (single or multi-level)
        cats: list[str] = []
        cat_node = ser.find(f"./{{{_NS['c']}}}cat")
        if cat_node is not None:
            pts: dict[int, list[str]] = {}
            for lvl in cat_node.findall(f".//{{{_NS['c']}}}lvl"):
                for pt in lvl.findall(f"./{{{_NS['c']}}}pt"):
                    idx = int(pt.attrib.get("idx", "0") or "0")
                    v = pt.find(f"./{{{_NS['c']}}}v")
                    if v is not None and v.text:
                        pts.setdefault(idx, []).append(v.text.strip())
            if pts:
                cats = [" / ".join(pts[k]) for k in sorted(pts)]
            else:
                pt_map: dict[int, str] = {}
                for pt in cat_node.findall(f".//{{{_NS['c']}}}pt"):
                    idx = int(pt.attrib.get("idx", "0") or "0")
                    v = pt.find(f"./{{{_NS['c']}}}v")
                    if v is not None and v.text:
                        pt_map[idx] = v.text.strip()
                cats = [pt_map[k] for k in sorted(pt_map)]
        if cats and not categories_master:
            categories_master = cats

        # Values + numFmt
        val_node = ser.find(f"./{{{_NS['c']}}}val")
        if val_node is None:
            val_node = ser.find(f"./{{{_NS['c']}}}yVal")
        vals: list[str] = []
        if val_node is not None:
            nc = val_node.find(f".//{{{_NS['c']}}}numCache")
            fmt_node = nc.find(f"./{{{_NS['c']}}}formatCode") if nc is not None else None
            fmt_code = fmt_node.text.strip() if (fmt_node is not None and fmt_node.text) else None
            if fmt_code is None:
                nf_node = ser.find(f".//{{{_NS['c']}}}numFmt")
                if nf_node is not None:
                    fmt_code = nf_node.attrib.get("formatCode")
            v_map: dict[int, str] = {}
            for pt in (nc.findall(f"./{{{_NS['c']}}}pt") if nc is not None else val_node.findall(f".//{{{_NS['c']}}}pt")):
                idx = int(pt.attrib.get("idx", "0") or "0")
                v = pt.find(f"./{{{_NS['c']}}}v")
                if v is not None and v.text:
                    v_map[idx] = _format_chart_number(v.text, fmt_code)
            vals = [v_map[k] for k in sorted(v_map)]
        if vals:
            series_rows.append((s_name, cats or categories_master, vals))

    if not series_rows:
        return []

    out = [header]
    cols = categories_master or [f"P{i+1}" for i in range(max(len(r[2]) for r in series_rows))]
    out.append("| Series | " + " | ".join(c.replace("|", "/") for c in cols) + " |")
    out.append("| --- | " + " | ".join("---" for _ in cols) + " |")
    for s_name, _c, vals in series_rows:
        padded = vals + [""] * max(0, len(cols) - len(vals))
        out.append(f"| {s_name.replace('|', '/')} | " + " | ".join(padded[: len(cols)]) + " |")
    return out


def _walk_sp_tree(node: ET.Element, *, depth: int, titles: list[str], body: list[str], tables: list[str], has_image: list[bool]) -> None:
    if depth > MAX_OOXML_GROUP_DEPTH:
        return
    for child in node:
        lname = _local(child.tag)
        if lname == "grpSp":
            _walk_sp_tree(child, depth=depth + 1, titles=titles, body=body, tables=tables, has_image=has_image)
        elif lname == "pic":
            if child.find(f".//{{{_NS['a']}}}blip") is not None:
                has_image[0] = True
        elif lname == "sp":
            ph = child.find(f".//{{{_NS['p']}}}ph")
            ph_type = ph.attrib.get("type", "") if ph is not None else ""
            lines = _extract_txbody_lines(child)
            if ph_type in ("title", "ctrTitle", "subTitle"):
                titles.extend(lines)
            else:
                body.extend(lines)
        elif lname == "graphicFrame":
            for tbl in child.findall(f".//{{{_NS['a']}}}tbl"):
                tables.extend(_extract_table_markdown(tbl))


@dataclass(frozen=True)
class PptxExtractionResult:
    text: str
    document_pages: dict[str, Any]
    slide_count: int
    visual_only_slides: list[int] = field(default_factory=list)
    hidden_slides: list[int] = field(default_factory=list)
    chart_count: int = 0
    open_gaps: list[str] = field(default_factory=list)


def extract_pptx(path: Path) -> PptxExtractionResult:
    """Extract ordered slides, CFO speaker notes, formatted charts, and SmartArt from a ``.pptx`` file."""
    file_size = path.stat().st_size
    if file_size > MAX_PPTX_UPLOAD_BYTES:
        raise OoxmlSecurityError("pptx_too_large", f"PPTX exceeds {MAX_PPTX_UPLOAD_BYTES} byte upload cap.")

    def _run() -> PptxExtractionResult:
        budget = _BudgetTracker()
        try:
            zf = zipfile.ZipFile(path, "r")
        except (zipfile.BadZipFile, OSError) as err:
            raise OoxmlSecurityError("pptx_invalid_archive", f"Invalid PPTX archive: {err}") from err

        with zf:
            names = zf.namelist()
            if len(names) > MAX_OOXML_MEMBERS:
                raise OoxmlSecurityError("ooxml_too_many_members", f"PPTX contains {len(names)} members (limit {MAX_OOXML_MEMBERS}).")
            if "ppt/presentation.xml" not in names:
                raise OoxmlSecurityError("pptx_missing_presentation", "PPTX is missing ppt/presentation.xml.")

            pres_root = safe_parse_xml(read_zip_member_capped(zf, "ppt/presentation.xml", budget))
            pres_rels = _parse_rels(zf, "ppt/_rels/presentation.xml.rels", "ppt", budget, ("ppt/",))

            slide_targets: list[str] = []
            for sld_id in pres_root.findall(f".//{{{_NS['p']}}}sldIdLst/{{{_NS['p']}}}sldId"):
                rid = sld_id.attrib.get(f"{{{_NS['r']}}}id", "").strip()
                if rid and rid in pres_rels:
                    _rtype, target = pres_rels[rid]
                    if target not in slide_targets:
                        slide_targets.append(target)

            if not slide_targets:
                # Fallback only if sldIdLst is absent
                slide_targets = sorted(
                    n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)
                )
            if len(slide_targets) > MAX_OOXML_SLIDES:
                raise OoxmlSecurityError("ooxml_too_many_slides", f"PPTX has {len(slide_targets)} slides (limit {MAX_OOXML_SLIDES}).")

            slide_parts: list[str] = []
            pages_meta: list[list[Any]] = []
            slide_info: dict[str, dict[str, Any]] = {}
            visual_only: list[int] = []
            hidden_slides: list[int] = []
            open_gaps: list[str] = []
            chart_count = 0
            offset = 0

            for slide_num, target in enumerate(slide_targets, 1):
                raw_slide = read_zip_member_capped(zf, target, budget)
                if not raw_slide:
                    continue
                s_root = safe_parse_xml(raw_slide)
                is_hidden = s_root.attrib.get("show") in ("0", "false")
                if is_hidden:
                    hidden_slides.append(slide_num)

                titles: list[str] = []
                body: list[str] = []
                tables: list[str] = []
                has_image = [False]
                sp_tree = s_root.find(f".//{{{_NS['p']}}}cSld/{{{_NS['p']}}}spTree")
                if sp_tree is not None:
                    _walk_sp_tree(sp_tree, depth=0, titles=titles, body=body, tables=tables, has_image=has_image)
                if not has_image[0] and s_root.find(f".//{{{_NS['a']}}}blip") is not None:
                    has_image[0] = True
                s_root.clear()

                # Per-slide relationships for Notes, Charts, and SmartArt
                slide_dir = posixpath.dirname(target)
                slide_base = posixpath.basename(target)
                rels_path = posixpath.join(slide_dir, "_rels", f"{slide_base}.rels")
                s_rels = _parse_rels(zf, rels_path, slide_dir, budget, ("ppt/",))

                notes_lines: list[str] = []
                chart_lines: list[str] = []
                smartart_lines: list[str] = []

                for _rid, (rtype, rel_target) in s_rels.items():
                    rt_low = rtype.lower()
                    if "/notesslide" in rt_low:
                        n_raw = read_zip_member_capped(zf, rel_target, budget)
                        if n_raw:
                            n_root = safe_parse_xml(n_raw)
                            for sp in n_root.findall(f".//{{{_NS['p']}}}sp"):
                                ph = sp.find(f".//{{{_NS['p']}}}ph")
                                if ph is not None and ph.attrib.get("type") == "sldNum":
                                    continue
                                notes_lines.extend(_extract_txbody_lines(sp))
                            n_root.clear()
                    elif "/chart" in rt_low:
                        c_raw = read_zip_member_capped(zf, rel_target, budget)
                        if c_raw:
                            parsed_chart = _parse_chart_xml(c_raw, slide_num)
                            if parsed_chart:
                                chart_count += 1
                                chart_lines.extend(parsed_chart)
                    elif "/diagramdata" in rt_low:
                        d_raw = read_zip_member_capped(zf, rel_target, budget)
                        if d_raw:
                            d_root = safe_parse_xml(d_raw)
                            for t_node in d_root.iter(f"{{{_NS['a']}}}t"):
                                if t_node.text and t_node.text.strip():
                                    smartart_lines.append(re.sub(r"\s+", " ", t_node.text.strip()))
                            d_root.clear()

                header_tag = f"[Slide {slide_num}" + (" | Hidden Slide]" if is_hidden else "]")
                sections: list[str] = [header_tag]
                if titles:
                    sections.append(" — ".join(dict.fromkeys(titles)))
                if body:
                    sections.extend(body)
                if smartart_lines:
                    sections.append("[SmartArt] " + " ; ".join(dict.fromkeys(smartart_lines)))
                if tables:
                    sections.extend(tables)
                if chart_lines:
                    sections.extend(chart_lines)
                if notes_lines:
                    sections.append(f"[Slide {slide_num} Notes] " + " ".join(notes_lines))

                content_body_chars = sum(len(s) for s in (*titles, *body, *smartart_lines, *tables, *chart_lines, *notes_lines))
                is_visual_only = content_body_chars < 30 and has_image[0]
                if is_visual_only:
                    visual_only.append(slide_num)
                    sections.append(f"[Slide {slide_num}: visual_only — pure image slide with no embedded text/table data]")
                    open_gaps.append(f"pptx_visual_only_slide:{slide_num}")

                slide_text = "\n".join(sections)
                start = offset
                end = start + len(slide_text)
                slide_parts.append(slide_text)
                pages_meta.append([start, end, "text"])
                slide_info[str(slide_num)] = {
                    "title": titles[0][:160] if titles else None,
                    "hidden": is_hidden,
                    "visual_only": is_visual_only,
                    "has_notes": bool(notes_lines),
                    "has_charts": bool(chart_lines),
                }
                offset = end + 1  # +1 for '\f'

            full_text = "\f".join(slide_parts) + ("\f" if slide_parts else "")
            doc_pages = {
                "page_count": len(pages_meta),
                "pages": pages_meta,
                "text_limit_reached": False,
                "original_retained": False,
                "method": "pptx_slides_v1",
                "version": PAGES_FORMAT_VERSION,
                "slides": slide_info,
                "visual_only_slides": visual_only,
                "hidden_slides": hidden_slides,
            }
            return PptxExtractionResult(
                text=full_text,
                document_pages=doc_pages,
                slide_count=len(pages_meta),
                visual_only_slides=visual_only,
                hidden_slides=hidden_slides,
                chart_count=chart_count,
                open_gaps=open_gaps,
            )

    if file_size > _HEAVY_GATE_THRESHOLD_BYTES:
        with heavy_memory_section():
            return _run()
    return _run()
