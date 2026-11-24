"""Deterministic non-earnings Form 8-K parser, structural shell stripper, and exhibit router.

Key invariants:
1. Item-level dual-route: mixed ``2.02 + non-2.02`` 8-Ks strip ``Item 2.02`` and keep all non-``2.02`` items.
2. Keeps ALL ``Item 8.01`` and ``Item 5.02`` disclosures (zero silent drops).
3. Strips Cover Page (before first ``Item \\d\\.\\d{2}``) and ``SIGNATURES`` tail without violent fixed truncation.
4. Full ``EX-16.1`` fetch on ``Item 4.01``; table-preserving ``EX-99.*`` via ``sec_deep.strip_html_preserving_tables``;
   ``LazyAttachmentRef`` only for ``EX-10.*`` / ``EX-4.*`` with 2–3 MB capped ``fetch_lazy_attachment``.
"""
from __future__ import annotations

from hashlib import sha256
import re
from threading import Event
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ....research_signals import LazyAttachmentRef, RawEventCandidate, SuppressedEventRecord, deterministic_signal_id
from ...policy import FinDataPolicy
from ..base import StreamTransport, Transport
from ..sec_deep import (
    _SAFE_FILENAME_RE,
    _IndexHtmParser,
    _clean_text,
    _decode_bytes,
    _normalize_items,
    strip_html_preserving_tables,
)
from .pacing import PacingTelemetry, RawSubmissionRow, SecPacingError, SecPacingLimiter, sec_get_bytes, sec_get_json, sec_get_text
from .rules import SecEventsRules

HARD_LAZY_MAX_BYTES = 3_000_000
DEFAULT_LAZY_MAX_BYTES = 2_500_000

_ITEM_HEADING_LINE_RE = re.compile(r"(?im)^\s*(?:\*\*|__)?\s*Item\s+(\d\.\d{2})\b[^\n]*")
_ITEM_ANY_RE = re.compile(r"(?i)\bItem\s+(\d\.\d{2})\b")
_SIGNATURES_LINE_RE = re.compile(r"(?im)^\s*(?:SIGNATURES?|SIGNATURE)\s*$")
_PURSUANT_SIGNATURES_RE = re.compile(
    r"(?im)^\s*Pursuant\s+to\s+the\s+requirements\s+of\s+the\s+Securities\s+Exchange\s+Act\s+of\s+1934\b"
)

_EX16_TYPE_RE = re.compile(r"^EX-?16(?:\.0*1)?$", re.IGNORECASE)
_EX16_NAME_RE = re.compile(r"(?:ex(?:h(?:ibit)?)?[-_.]?16[-_.]?1|(?<![0-9])16[-_.]1)(?:[^0-9]|$)", re.IGNORECASE)
_EX99_TYPE_RE = re.compile(r"^EX-?99(?:\.\d+)?$", re.IGNORECASE)
_EX99_NAME_RE = re.compile(r"(?:ex(?:h(?:ibit)?)?[-_.]?99[-_.]?\d+|(?<![0-9])99[-_.]\d+)(?:[^0-9]|$)", re.IGNORECASE)
_EX10_TYPE_RE = re.compile(r"^EX-?10(?:\.\d+[A-Za-z]?)?$", re.IGNORECASE)
_EX10_NAME_RE = re.compile(r"(?:ex(?:h(?:ibit)?)?[-_.]?10[-_.]?\d*|(?<![0-9])10[-_.]\d+)(?:[^0-9]|$)", re.IGNORECASE)
_EX4_TYPE_RE = re.compile(r"^EX-?4(?:\.\d+[A-Za-z]?)?$", re.IGNORECASE)
_EX4_NAME_RE = re.compile(r"(?:ex(?:h(?:ibit)?)?[-_.]?4[-_.]?\d*|(?<![0-9])4[-_.]\d+)(?:[^0-9]|$)", re.IGNORECASE)
_EX2_TYPE_RE = re.compile(r"^EX-?2(?:\.\d+[A-Za-z]?)?$", re.IGNORECASE)
_TEXT_EXT_RE = re.compile(r"\.(?:html?|txt|xml)$", re.IGNORECASE)
_HTML_LIKELY_RE = re.compile(r"<(?:html|body|div|table|p|span|ix:|tr|td)\b", re.IGNORECASE)
_EARNINGS_EX99_DESC_RE = re.compile(r"\b(?:earnings\s+release|financial\s+results|quarterly\s+results)\b", re.IGNORECASE)


class LazyAttachmentContent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str
    filename: str
    attachment_role: str
    sec_url: str
    text: str
    content_sha256: str
    byte_length: int


class ParsedExhibitMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    filename: str
    exhibit_type: str | None = None
    attachment_role: Literal[
        "ex16_auditor_letter",
        "ex99_press_release",
        "ex10_material_contract",
        "ex4_debt_indenture",
        "other_exhibit",
    ] = "other_exhibit"
    sec_url: str
    description: str | None = None


class Raw8kParsed(BaseModel):
    """Unfiltered deterministic 8-K parsing record persisted in ``sec_events_l1_raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cik: str
    accession: str
    form: str
    filed_at: str
    event_date: str | None = None
    primary_document: str | None = None
    sec_url: str
    fetched_full_content: bool = False
    raw_items: list[str] = Field(default_factory=list)
    item_sections: dict[str, str] = Field(default_factory=dict)
    stripped_primary_text: str = ""
    primary_sha256: str | None = None
    ex16_text: str | None = None
    ex16_filename: str | None = None
    ex16_sha256: str | None = None
    ex99_texts: dict[str, str] = Field(default_factory=dict)
    ex99_excerpts: dict[str, str] = Field(default_factory=dict)
    ex99_descriptions: dict[str, str | None] = Field(default_factory=dict)
    exhibits: list[ParsedExhibitMeta] = Field(default_factory=list)


def strip_cover_and_signatures(text: str) -> str:
    """Strip SEC 8-K Cover Page (before first ``Item d.dd``) and ``SIGNATURES`` block."""
    raw = (text or "").strip()
    if not raw:
        return ""
    m_start = _ITEM_HEADING_LINE_RE.search(raw) or _ITEM_ANY_RE.search(raw)
    start_idx = m_start.start() if m_start else 0
    body = raw[start_idx:]

    sig_match = _SIGNATURES_LINE_RE.search(body)
    if sig_match:
        body = body[: sig_match.start()]
    purs_match = _PURSUANT_SIGNATURES_RE.search(body)
    if purs_match and purs_match.start() > len(body) // 3:
        body = body[: purs_match.start()]
    return body.strip()


def slice_8k_items(text: str) -> tuple[dict[str, str], str]:
    """Strip cover/signatures and slice 8-K text per ``Item d.dd``."""
    stripped = strip_cover_and_signatures(text)
    if not stripped:
        return {}, ""
    matches = list(_ITEM_HEADING_LINE_RE.finditer(stripped))
    if not matches:
        matches = list(_ITEM_ANY_RE.finditer(stripped))
    if not matches:
        return {}, stripped

    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        code = match.group(1).strip()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(stripped)
        chunk = stripped[start:end].strip()
        if not chunk:
            continue
        if code in sections:
            sections[code] = f"{sections[code]}\n\n{chunk}"
        else:
            sections[code] = chunk
    return sections, stripped


def classify_8k_items(
    raw_items: Sequence[str],
    rules: SecEventsRules,
) -> tuple[str, list[str], bool]:
    """Return ``(decision, kept_items, mixed_earnings_stripped)``.

    ``decision`` is one of:
    - ``"routed_to_earnings_engine"``: pure earnings 8-K (``2.02``, ``2.02+9.01``, or ``2.02+7.01``)
    - ``"low_materiality_filtered"``: only low-value statutory items (e.g. ``5.07``, ``5.03``, ``3.02``, ``3.03``, ``9.01``)
    - ``"keep"``: contains at least one material non-earnings item (including ALL ``8.01`` & ``5.02``)
    """
    norm = [str(i).strip() for i in raw_items if str(i).strip()]
    ordered_unique = list(dict.fromkeys(norm))
    if not ordered_unique:
        return "low_materiality_filtered", [], False

    earnings_only = set(rules.form8k.earnings_only_items)
    companions = set(rules.form8k.earnings_companion_items)
    low_mat = set(rules.form8k.low_materiality_only_items)

    non_901 = [i for i in ordered_unique if i != "9.01"]
    if not non_901:
        return "low_materiality_filtered", [], False

    has_202 = any(i in earnings_only for i in non_901)
    substantive_non_earnings = [i for i in non_901 if i not in earnings_only and i not in companions]

    if has_202 and not substantive_non_earnings:
        return "routed_to_earnings_engine", [], False

    if all(i in low_mat for i in non_901):
        return "low_materiality_filtered", [], False

    # Keep all non-2.02 substantive items (and keep 7.01 unless paired solely with 2.02)
    kept = [i for i in ordered_unique if i not in earnings_only and i != "9.01"]
    if not kept:
        return "low_materiality_filtered", [], False
    return "keep", kept, has_202


def _classify_exhibit(
    filename: str,
    exhibit_type: str,
    description: str | None,
) -> Literal["ex16_auditor_letter", "ex99_press_release", "ex10_material_contract", "ex4_debt_indenture", "other_exhibit"] | None:
    et = (exhibit_type or "").strip()
    desc = (description or "").strip()
    if _EX16_TYPE_RE.fullmatch(et) or _EX16_NAME_RE.search(filename) or ("16.1" in et):
        return "ex16_auditor_letter"
    if _EX10_TYPE_RE.fullmatch(et) or _EX10_NAME_RE.search(filename):
        return "ex10_material_contract"
    if _EX4_TYPE_RE.fullmatch(et) or _EX4_NAME_RE.search(filename):
        return "ex4_debt_indenture"
    if _EX99_TYPE_RE.fullmatch(et) or _EX99_NAME_RE.search(filename):
        return "ex99_press_release"
    if _EX2_TYPE_RE.fullmatch(et) or re.search(r"(?:^|[^0-9])ex[-_.]?2[-_.]?\d", filename, re.IGNORECASE):
        return "other_exhibit"
    if desc and ("auditor" in desc.lower() or "16.1" in desc.lower()) and _TEXT_EXT_RE.search(filename):
        return "ex16_auditor_letter"
    return None


def discover_filing_exhibits(
    cik: str,
    nodash: str,
    *,
    primary_document: str | None,
    dashed_accession: str | None = None,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    policy: FinDataPolicy | None,
    transport: Transport | None,
    stream_transport: StreamTransport | None,
    cancel_event: Event | None,
    deadline: float | None,
    telemetry: PacingTelemetry | None,
) -> tuple[list[ParsedExhibitMeta], list[str] | None]:
    """Discover filing exhibits from ``index.json`` with fallback to ``*-index.htm``."""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}"
    index_json_url = f"{base_url}/index.json"
    exhibits: list[ParsedExhibitMeta] = []
    seen_files: set[str] = set()
    index_htm: str | None = f"{dashed_accession}-index.htm" if dashed_accession else None
    items_hint: list[str] | None = None

    try:
        idx_doc, _sha = sec_get_json(
            index_json_url,
            category="filings",
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
        )
        if isinstance(idx_doc, Mapping):
            directory = idx_doc.get("directory")
            if isinstance(directory, Mapping):
                items_hint = _normalize_items(directory.get("items") or idx_doc.get("items"))
                entries = directory.get("item")
                if isinstance(entries, list):
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            continue
                        name = entry.get("name")
                        if not isinstance(name, str) or not _SAFE_FILENAME_RE.fullmatch(name):
                            continue
                        if name.lower().endswith(("-index.htm", "-index.html")):
                            index_htm = name
                            continue
                        if primary_document and name == primary_document:
                            continue
                        etype = str(entry.get("type") or entry.get("document_type") or "").strip()
                        desc = str(entry.get("description") or "").strip() or None
                        role = _classify_exhibit(name, etype, desc)
                        if role is not None and name not in seen_files:
                            seen_files.add(name)
                            exhibits.append(
                                ParsedExhibitMeta(
                                    filename=name,
                                    exhibit_type=etype or None,
                                    attachment_role=role,
                                    sec_url=f"{base_url}/{name}",
                                    description=desc,
                                )
                            )
    except SecPacingError as err:
        if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
            raise
    except Exception:
        pass

    if not exhibits and index_htm:
        try:
            htm_text, _ct, _sha = sec_get_text(
                f"{base_url}/{index_htm}",
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
            parser = _IndexHtmParser()
            parser.feed(htm_text or "")
            parser.close()
            if items_hint is None:
                found = re.findall(r"\bItem\s+(\d+\.\d{2})\b", _clean_text(" ".join(parser.text_parts)), re.IGNORECASE)
                if found:
                    items_hint = list(dict.fromkeys(found))
            for row in parser.rows:
                if len(row) < 4:
                    continue
                desc = row[1].strip() if row[1] else None
                doc_name = row[2].split()[0] if row[2] else ""
                doc_type = row[3].strip() if row[3] else ""
                if not doc_name or not _SAFE_FILENAME_RE.fullmatch(doc_name) or doc_name == primary_document:
                    continue
                role = _classify_exhibit(doc_name, doc_type, desc)
                if role is not None and doc_name not in seen_files:
                    seen_files.add(doc_name)
                    exhibits.append(
                        ParsedExhibitMeta(
                            filename=doc_name,
                            exhibit_type=doc_type or None,
                            attachment_role=role,
                            sec_url=f"{base_url}/{doc_name}",
                            description=desc,
                        )
                    )
        except SecPacingError as err:
            if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
                raise
        except Exception:
            pass

    return exhibits, items_hint


def condense_ex99_preserving_tables(
    cleaned_text: str,
    tables_markdown: Sequence[str],
    *,
    keywords: Sequence[str],
    max_chars: int = 12_000,
) -> str:
    """Build condensed EX-99 excerpt: first 2 prose paragraphs + tables + high-density paragraphs."""
    blocks = [b.strip() for b in re.split(r"\n{2,}", cleaned_text or "") if b.strip()]
    selected: list[str] = []
    seen: set[str] = set()

    prose_blocks = [b for b in blocks if not b.startswith("|")]
    for p in prose_blocks[:2]:
        if p not in seen:
            seen.add(p)
            selected.append(p)

    for tbl in tables_markdown:
        t = tbl.strip()
        if t and t not in seen:
            seen.add(t)
            selected.append(t)

    kw_lower = tuple(k.lower() for k in keywords if k)
    number_or_pct_re = re.compile(r"(?:\$\s*\d|\d+(?:\.\d+)?\s*%|\b(?:million|billion)\b)", re.IGNORECASE)
    for p in prose_blocks[2:]:
        if p in seen:
            continue
        low = p.lower()
        if number_or_pct_re.search(p) or any(kw in low for kw in kw_lower):
            seen.add(p)
            selected.append(p)

    combined = "\n\n".join(selected).strip()
    if len(combined) > max_chars:
        return combined[: max_chars - 32].rstrip() + "\n...[EX-99 excerpt truncated]"
    return combined


def parse_8k_raw(
    row: RawSubmissionRow,
    rules: SecEventsRules,
    *,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    policy: FinDataPolicy | None,
    transport: Transport | None,
    stream_transport: StreamTransport | None,
    cancel_event: Event | None,
    deadline: float | None,
    telemetry: PacingTelemetry | None,
) -> Raw8kParsed:
    """Fetch & deterministically parse one 8-K / 8-K/A into unfiltered ``Raw8kParsed``.

    Zero-network optimization: when ``row.items`` is already known from ``submissions`` and
    marks the filing purely ``routed_to_earnings_engine`` or ``low_materiality_filtered``,
    returns immediately without downloading HTML/exhibits.
    """
    sec_url = row.primary_document_url or row.index_url or f"https://www.sec.gov/Archives/edgar/data/{int(row.cik)}/{row.nodash}"
    if row.items is not None:
        decision, _kept, _mixed = classify_8k_items(row.items, rules)
        if decision != "keep":
            return Raw8kParsed(
                cik=row.cik,
                accession=row.accession,
                form=row.form,
                filed_at=row.filed_at.isoformat(),
                event_date=row.period_of_report.isoformat() if row.period_of_report else None,
                primary_document=row.primary_document,
                sec_url=sec_url,
                fetched_full_content=False,
                raw_items=list(row.items),
            )

    if not row.primary_document_url:
        raise ValueError(f"8-K {row.accession} has no primaryDocument URL.")

    primary_html, _ct, primary_sha = sec_get_text(
        row.primary_document_url,
        category="filings",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        max_bytes=15_000_000,
        large=True,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    cleaned_primary, _tables = strip_html_preserving_tables(primary_html) if _HTML_LIKELY_RE.search(primary_html[:8192]) else (primary_html, [])
    item_sections, stripped_body = slice_8k_items(cleaned_primary)

    discovered_items = list(row.items) if row.items is not None else []
    if not discovered_items and item_sections:
        discovered_items = list(item_sections.keys())

    exhibits, idx_items = discover_filing_exhibits(
        row.cik,
        row.nodash,
        primary_document=row.primary_document,
        dashed_accession=row.accession,
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    if not discovered_items and idx_items:
        discovered_items = list(idx_items)

    ex16_text: str | None = None
    ex16_filename: str | None = None
    ex16_sha256: str | None = None
    ex99_texts: dict[str, str] = {}
    ex99_excerpts: dict[str, str] = {}
    ex99_descriptions: dict[str, str | None] = {}

    has_item_401 = "4.01" in discovered_items or "4.01" in item_sections
    for ex in exhibits:
        if ex.attachment_role == "ex16_auditor_letter" and (has_item_401 or ex16_text is None):
            ex_raw, _ex_ct, ex_sha = sec_get_text(
                ex.sec_url,
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                max_bytes=5_000_000,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
            cleaned_ex16, _ = strip_html_preserving_tables(ex_raw) if _HTML_LIKELY_RE.search(ex_raw[:8192]) else (ex_raw, [])
            ex16_text = cleaned_ex16.strip()
            ex16_filename = ex.filename
            ex16_sha256 = ex_sha
        elif ex.attachment_role == "ex99_press_release" and _TEXT_EXT_RE.search(ex.filename):
            # Only pre-fetch EX-99.* when it is not purely an Item 2.02 earnings release in a mixed 8-K
            if not _should_fetch_ex99(discovered_items, item_sections, ex):
                continue
            ex_raw, _ex_ct, _ex_sha = sec_get_text(
                ex.sec_url,
                category="filings",
                user_agent=user_agent,
                limiter=limiter,
                policy=policy,
                transport=transport,
                stream_transport=stream_transport,
                max_bytes=10_000_000,
                large=True,
                cancel_event=cancel_event,
                deadline=deadline,
                telemetry=telemetry,
            )
            cleaned_ex99, parsed_tables = strip_html_preserving_tables(ex_raw) if _HTML_LIKELY_RE.search(ex_raw[:8192]) else (ex_raw, [])
            ex99_texts[ex.filename] = cleaned_ex99
            ex99_excerpts[ex.filename] = condense_ex99_preserving_tables(
                cleaned_ex99,
                [t.raw_markdown for t in parsed_tables if t.raw_markdown],
                keywords=rules.form8k.ex99_high_density_keywords,
                max_chars=rules.form8k.ex99_max_excerpt_chars,
            )
            ex99_descriptions[ex.filename] = ex.description

    return Raw8kParsed(
        cik=row.cik,
        accession=row.accession,
        form=row.form,
        filed_at=row.filed_at.isoformat(),
        event_date=row.period_of_report.isoformat() if row.period_of_report else None,
        primary_document=row.primary_document,
        sec_url=sec_url,
        fetched_full_content=True,
        raw_items=discovered_items,
        item_sections=item_sections,
        stripped_primary_text=stripped_body,
        primary_sha256=primary_sha,
        ex16_text=ex16_text,
        ex16_filename=ex16_filename,
        ex16_sha256=ex16_sha256,
        ex99_texts=ex99_texts,
        ex99_excerpts=ex99_excerpts,
        ex99_descriptions=ex99_descriptions,
        exhibits=exhibits,
    )


def _should_fetch_ex99(
    items: Sequence[str],
    item_sections: Mapping[str, str],
    ex: ParsedExhibitMeta,
) -> bool:
    if "2.02" not in items:
        return True
    non_202_text = "\n".join(text for code, text in item_sections.items() if code not in ("2.02", "9.01"))
    ex_num = ex.exhibit_type or ""
    ex_num_norm = ex_num.upper().replace("EX-", "").strip()
    if ex_num_norm and re.search(rf"\bExhibit\s*{re.escape(ex_num_norm)}\b", non_202_text, re.IGNORECASE):
        return True
    if ex.filename and ex.filename.lower() in non_202_text.lower():
        return True
    if ex.description and _EARNINGS_EX99_DESC_RE.search(ex.description):
        return False
    # If Item 2.02 explicitly mentions this exhibit and non-2.02 does not, skip to avoid re-ingesting earnings release
    text_202 = item_sections.get("2.02", "")
    if ex_num_norm and re.search(rf"\bExhibit\s*{re.escape(ex_num_norm)}\b", text_202, re.IGNORECASE):
        return False
    return True


def apply_8k_rules(
    raw: Raw8kParsed,
    rules: SecEventsRules,
    *,
    symbol: str | None = None,
    company_name: str | None = None,
    is_context_only: bool = False,
) -> tuple[RawEventCandidate | None, SuppressedEventRecord | None, dict[str, str]]:
    """Apply externalized 8-K rules to unfiltered ``Raw8kParsed`` (0 SEC requests)."""
    filed_d = _parse_iso_date(raw.filed_at)
    event_d = _parse_iso_date(raw.event_date)
    effective_items = list(raw.raw_items) if raw.raw_items else list(raw.item_sections.keys())
    decision, kept_items, mixed_202 = classify_8k_items(effective_items, rules)

    if decision != "keep":
        suppressed = SuppressedEventRecord(
            accession=raw.accession,
            form=raw.form,
            filed_at=filed_d,
            category="material_8k",
            suppression_reason=decision,
            items=effective_items,
            summary_hint=f"8-K items {', '.join(effective_items) or 'none'} filtered by {decision}",
        )
        return None, suppressed, {}

    nodash = raw.accession.replace("-", "")
    source_id = f"sec_event:{raw.cik}:{nodash}:8k"
    source_texts: dict[str, str] = {}

    # Build stripped primary body keeping ONLY non-2.02 and non-9.01 slices when item_sections exist
    if raw.item_sections:
        kept_slices = [
            sec_text
            for code, sec_text in raw.item_sections.items()
            if code not in set(rules.form8k.earnings_only_items) and code != "9.01"
        ]
        primary_body = "\n\n".join(kept_slices).strip() if kept_slices else raw.stripped_primary_text
    else:
        primary_body = raw.stripped_primary_text

    excerpt_parts: list[str] = []
    if primary_body:
        excerpt_parts.append(primary_body[: rules.form8k.primary_max_excerpt_chars])
        source_texts[source_id] = primary_body

    # Item 4.01 EX-16.1: ALWAYS include 100% full text
    if raw.ex16_text:
        ex16_block = f"=== EX-16.1 ({raw.ex16_filename or 'auditor_letter'}) ===\n{raw.ex16_text}"
        excerpt_parts.append(ex16_block)
        ex16_source_id = f"sec_event:{raw.cik}:{nodash}:ex16"
        source_texts[ex16_source_id] = raw.ex16_text
        if source_id in source_texts:
            source_texts[source_id] = f"{source_texts[source_id]}\n\n{ex16_block}"
        else:
            source_texts[source_id] = ex16_block

    exhibit_source_ids: dict[str, str] = {}
    for fname, full_text in raw.ex99_texts.items():
        ex_sid = f"sec_event:{raw.cik}:{nodash}:ex99:{fname}"
        source_texts[ex_sid] = full_text
        exhibit_source_ids[fname] = ex_sid
        condensed = raw.ex99_excerpts.get(fname) or full_text[: rules.form8k.ex99_max_excerpt_chars]
        excerpt_parts.append(f"=== {fname} ===\n{condensed}")

    full_excerpt = "\n\n".join(p for p in excerpt_parts if p).strip()
    if source_id not in source_texts:
        source_texts[source_id] = full_excerpt

    lazy_refs: list[LazyAttachmentRef] = []
    for ex in raw.exhibits:
        if ex.attachment_role in ("ex10_material_contract", "ex4_debt_indenture", "other_exhibit"):
            lazy_refs.append(
                LazyAttachmentRef(
                    source_id=f"sec_event:{raw.cik}:{nodash}:lazy:{ex.filename}",
                    filename=ex.filename,
                    exhibit_type=ex.exhibit_type,
                    attachment_role=ex.attachment_role,
                    sec_url=ex.sec_url,
                    description=ex.description,
                )
            )

    statutory_crit = [i for i in kept_items if i in set(rules.form8k.statutory_critical_items)]
    lag_days = (filed_d - event_d).days if event_d is not None else None
    thread_anchor = event_d.isoformat() if event_d else raw.filed_at
    thread_key = f"8k:{raw.cik}:{thread_anchor}:{','.join(sorted(kept_items))}"

    facts: dict[str, Any] = {
        "original_items": effective_items,
        "kept_items": kept_items,
        "mixed_earnings_202_stripped": mixed_202,
        "earnings_channel_also_routed": mixed_202,
        "has_ex16_auditor_letter": bool(raw.ex16_text),
        "exhibit_source_ids": exhibit_source_ids,
        "lazy_attachment_count": len(lazy_refs),
        "is_amendment": raw.form.endswith("/A"),
    }

    cand = RawEventCandidate(
        candidate_id=deterministic_signal_id("8k", raw.cik, raw.accession),
        cik=raw.cik,
        symbol=symbol,
        company_name=company_name,
        category="material_8k",
        form=raw.form,
        accession=raw.accession,
        amends_accession=None,
        event_thread_key=thread_key,
        filed_at=filed_d,
        event_date=event_d,
        reporting_lag_days=lag_days,
        items=kept_items,
        statutory_critical_items=statutory_crit,
        facts=facts,
        excerpt=full_excerpt,
        source_id=source_id,
        sec_url=raw.sec_url,
        filename=raw.primary_document,
        content_sha256=raw.primary_sha256 or sha256(full_excerpt.encode("utf-8")).hexdigest(),
        lazy_attachments=lazy_refs,
        is_context_only=is_context_only,
    )
    return cand, None, source_texts


def fetch_lazy_attachment(
    ref: LazyAttachmentRef,
    *,
    user_agent: str | None,
    max_bytes: int = DEFAULT_LAZY_MAX_BYTES,
    limiter: SecPacingLimiter | None = None,
    policy: FinDataPolicy | None = None,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
    cancel_event: Event | None = None,
    deadline: float | None = None,
    telemetry: PacingTelemetry | None = None,
) -> LazyAttachmentContent:
    """On-demand lazy loader for ``EX-10.*`` / ``EX-4.*`` capped strictly at 2–3 MB via ``_CappedSink``."""
    capped_limit = min(max(100_000, int(max_bytes)), HARD_LAZY_MAX_BYTES)
    raw_bytes, ct, digest = sec_get_bytes(
        ref.sec_url,
        category="filings",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        accept="text/html, text/plain, application/xml",
        max_bytes=capped_limit,
        large=True,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    decoded = _decode_bytes(raw_bytes, ct)
    cleaned = strip_html_preserving_tables(decoded)[0] if _HTML_LIKELY_RE.search(decoded[:8192]) else decoded.strip()
    return LazyAttachmentContent(
        source_id=ref.source_id,
        filename=ref.filename,
        attachment_role=ref.attachment_role,
        sec_url=ref.sec_url,
        text=cleaned,
        content_sha256=digest,
        byte_length=len(raw_bytes),
    )


def _parse_iso_date(value: str | None):
    from datetime import date

    if not value:
        return None
    return date.fromisoformat(value[:10])


__all__ = [
    "DEFAULT_LAZY_MAX_BYTES",
    "HARD_LAZY_MAX_BYTES",
    "LazyAttachmentContent",
    "ParsedExhibitMeta",
    "Raw8kParsed",
    "apply_8k_rules",
    "classify_8k_items",
    "condense_ex99_preserving_tables",
    "discover_filing_exhibits",
    "fetch_lazy_attachment",
    "parse_8k_raw",
    "slice_8k_items",
    "strip_cover_and_signatures",
]
