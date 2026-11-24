"""Deterministic FPI Form 6-K / 6-K/A parser, earnings router, and routine-noise pre-screener."""
from __future__ import annotations

from datetime import date
from hashlib import sha256
import re
import subprocess
from threading import Event
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ....research_signals import LazyAttachmentRef, RawEventCandidate, SuppressedEventRecord, deterministic_signal_id
from ...policy import FinDataPolicy
from ..base import StreamTransport, Transport
from ..sec_deep import strip_html_preserving_tables
from .form8k import ParsedExhibitMeta, _SAFE_FILENAME_RE, condense_ex99_preserving_tables, discover_filing_exhibits
from .pacing import PacingTelemetry, RawSubmissionRow, SecPacingError, SecPacingLimiter, sec_get_bytes, sec_get_json, sec_get_text
from .rules import SecEventsRules

_HTML_LIKELY_RE = re.compile(r"<(?:html|body|div|table|p|span|tr|td)\b", re.IGNORECASE)
_SIGNATURES_6K_RE = re.compile(r"(?im)^\s*(?:SIGNATURES?|SIGNATORIES)\s*$")
_BOARD_MEETING_RE = re.compile(r"\b(?:date|notice)\s+of\s+board\s+meeting\b", re.IGNORECASE)
_XBRL_REPORT_HTM_RE = re.compile(r"^r\d+\.htm$", re.IGNORECASE)
_COVER_PAGE_THRESHOLD_CHARS = 2000
_MAX_FALLBACK_HTML_FILES = 3
_MAX_FALLBACK_PDF_FILES = 2
_MAX_FALLBACK_PDF_BYTES = 12_000_000
_MAX_FALLBACK_PDF_PAGES = 20
_MAX_FALLBACK_PDF_CHARS = 80_000


class Raw6kParsed(BaseModel):
    """Unfiltered deterministic 6-K / 6-K/A parse persisted in ``sec_events_l1_raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cik: str
    accession: str
    form: str
    filed_at: str
    event_date: str | None = None
    primary_document: str | None = None
    description: str | None = None
    sec_url: str
    primary_text: str = ""
    primary_sha256: str | None = None
    ex99_texts: dict[str, str] = Field(default_factory=dict)
    ex99_excerpts: dict[str, str] = Field(default_factory=dict)
    ex99_descriptions: dict[str, str | None] = Field(default_factory=dict)
    exhibits: list[ParsedExhibitMeta] = Field(default_factory=list)


def classify_6k_text(headline_and_opening: str, rules: SecEventsRules) -> str | None:
    """Return suppression/routing reason if 6-K matches earnings or routine noise rules, else ``None``."""
    text = headline_and_opening or ""
    if _BOARD_MEETING_RE.search(text):
        for pat in rules.form6k.routine_noise_patterns:
            if re.search(pat.pattern, text, re.IGNORECASE):
                return pat.rule_id
    for pat_str in rules.form6k.earnings_patterns:
        if re.search(pat_str, text, re.IGNORECASE):
            return "routed_to_earnings_engine"
    for pat in rules.form6k.routine_noise_patterns:
        if re.search(pat.pattern, text, re.IGNORECASE):
            return pat.rule_id
    return None


def _strip_6k_tail(text: str) -> str:
    raw = (text or "").strip()
    m = _SIGNATURES_6K_RE.search(raw)
    if m and m.start() > 120:
        return raw[: m.start()].strip()
    return raw


def _extract_pdf_text(
    pdf_bytes: bytes,
    *,
    max_pages: int = _MAX_FALLBACK_PDF_PAGES,
    max_chars: int = _MAX_FALLBACK_PDF_CHARS,
) -> str:
    """Extract plain text from a 6-K PDF exhibit via ``pdftotext`` (poppler-utils)."""
    if not pdf_bytes:
        return ""
    try:
        proc = subprocess.run(
            ["pdftotext", "-l", str(max_pages), "-", "-"],
            input=pdf_bytes,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return ""
    if proc.returncode != 0 or not proc.stdout:
        return ""
    text = proc.stdout.decode("utf-8", errors="replace").replace("\x00", "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:max_chars]


def _entry_size_int(entry: Mapping[str, Any]) -> int:
    raw_sz = entry.get("size")
    if isinstance(raw_sz, int):
        return raw_sz
    if isinstance(raw_sz, str) and raw_sz.strip().isdigit():
        return int(raw_sz.strip())
    return 0


def _discover_6k_cover_fallback_files(
    row: RawSubmissionRow,
    *,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    policy: FinDataPolicy | None,
    transport: Transport | None,
    stream_transport: StreamTransport | None,
    cancel_event: Event | None,
    deadline: float | None,
    telemetry: PacingTelemetry | None,
) -> tuple[list[tuple[str, str, str | None]], list[tuple[str, str, str | None]]]:
    """Return ``(html_files, pdf_files)`` as ``(filename, url, description)`` sorted by size desc."""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(row.cik)}/{row.nodash}"
    try:
        idx_doc, _sha = sec_get_json(
            f"{base_url}/index.json",
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
    except SecPacingError as err:
        if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
            raise
        return [], []
    except Exception:
        return [], []

    directory = idx_doc.get("directory") if isinstance(idx_doc, Mapping) else None
    entries = directory.get("item") if isinstance(directory, Mapping) else None
    if not isinstance(entries, list):
        return [], []

    html_ranked: list[tuple[int, str, str, str | None]] = []
    pdf_ranked: list[tuple[int, str, str, str | None]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _SAFE_FILENAME_RE.fullmatch(name):
            continue
        low = name.lower()
        if row.primary_document and name == row.primary_document:
            continue
        if low.endswith(("-index.htm", "-index.html", "-index-headers.html")):
            continue
        if _XBRL_REPORT_HTM_RE.match(name):
            continue
        desc = str(entry.get("description") or "").strip() or None
        sz = _entry_size_int(entry)
        if low.endswith((".htm", ".html")):
            html_ranked.append((sz, name, f"{base_url}/{name}", desc))
        elif low.endswith(".pdf"):
            pdf_ranked.append((sz, name, f"{base_url}/{name}", desc))

    html_ranked.sort(key=lambda item: (-item[0], item[1]))
    pdf_ranked.sort(key=lambda item: (-item[0], item[1]))
    return (
        [(n, u, d) for _sz, n, u, d in html_ranked[:_MAX_FALLBACK_HTML_FILES]],
        [(n, u, d) for _sz, n, u, d in pdf_ranked[:_MAX_FALLBACK_PDF_FILES]],
    )


def parse_6k_raw(
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
) -> Raw6kParsed:
    """Fetch & deterministically parse one 6-K / 6-K/A + ``EX-99.*`` exhibits."""
    if not row.primary_document_url:
        raise ValueError(f"6-K {row.accession} has no primaryDocument URL.")

    raw_primary, _ct, p_sha = sec_get_text(
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
    cleaned_primary = (
        strip_html_preserving_tables(raw_primary)[0]
        if _HTML_LIKELY_RE.search(raw_primary[:8192])
        else raw_primary
    )
    cleaned_primary = _strip_6k_tail(cleaned_primary)

    exhibits, _items = discover_filing_exhibits(
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

    ex99_texts: dict[str, str] = {}
    ex99_excerpts: dict[str, str] = {}
    ex99_descriptions: dict[str, str | None] = {}
    for ex in exhibits:
        if ex.attachment_role == "ex99_press_release":
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
            cleaned_ex99, parsed_tables = (
                strip_html_preserving_tables(ex_raw)
                if _HTML_LIKELY_RE.search(ex_raw[:8192])
                else (ex_raw, [])
            )
            ex99_texts[ex.filename] = cleaned_ex99
            ex99_excerpts[ex.filename] = condense_ex99_preserving_tables(
                cleaned_ex99,
                [t.raw_markdown for t in parsed_tables if t.raw_markdown],
                keywords=rules.form8k.ex99_high_density_keywords,
                max_chars=rules.form8k.ex99_max_excerpt_chars,
            )
            ex99_descriptions[ex.filename] = ex.description

    # v6.1 P0-3: Cover-page-only 6-K fallback — fetch non-ex99 HTML or PDF exhibits in same SEC directory
    if len(cleaned_primary) < _COVER_PAGE_THRESHOLD_CHARS and not ex99_texts:
        fb_html, fb_pdf = _discover_6k_cover_fallback_files(
            row,
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
        )
        known_names = {ex.filename for ex in exhibits}
        for fname, furl, fdesc in fb_html:
            try:
                ex_raw, _ex_ct, _ex_sha = sec_get_text(
                    furl,
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
            except SecPacingError as err:
                if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
                    raise
                continue
            except Exception:
                continue
            cleaned_fb, parsed_tables = (
                strip_html_preserving_tables(ex_raw)
                if _HTML_LIKELY_RE.search(ex_raw[:8192])
                else (ex_raw, [])
            )
            cleaned_fb = cleaned_fb.strip()
            if not cleaned_fb:
                continue
            ex99_texts[fname] = cleaned_fb
            ex99_excerpts[fname] = condense_ex99_preserving_tables(
                cleaned_fb,
                [t.raw_markdown for t in parsed_tables if t.raw_markdown],
                keywords=rules.form8k.ex99_high_density_keywords,
                max_chars=rules.form8k.ex99_max_excerpt_chars,
            )
            ex99_descriptions[fname] = fdesc or "6-K fallback HTML exhibit"
            if fname not in known_names:
                known_names.add(fname)
                exhibits.append(
                    ParsedExhibitMeta(
                        filename=fname,
                        exhibit_type="6-K-HTML",
                        attachment_role="ex99_press_release",
                        sec_url=furl,
                        description=fdesc,
                    )
                )

        if not ex99_texts and fb_pdf:
            for fname, furl, fdesc in fb_pdf:
                try:
                    pdf_bytes, _pdf_ct, _pdf_sha = sec_get_bytes(
                        furl,
                        accept="application/pdf, */*",
                        category="filings",
                        user_agent=user_agent,
                        limiter=limiter,
                        policy=policy,
                        transport=transport,
                        stream_transport=stream_transport,
                        max_bytes=_MAX_FALLBACK_PDF_BYTES,
                        large=True,
                        cancel_event=cancel_event,
                        deadline=deadline,
                        telemetry=telemetry,
                    )
                except SecPacingError as err:
                    if err.reason in ("cancelled", "deadline_exceeded", "forbidden", "sec_contact_unconfigured"):
                        raise
                    continue
                except Exception:
                    continue
                pdf_text = _extract_pdf_text(pdf_bytes)
                if not pdf_text:
                    continue
                ex99_texts[fname] = pdf_text
                ex99_excerpts[fname] = condense_ex99_preserving_tables(
                    pdf_text,
                    [],
                    keywords=rules.form8k.ex99_high_density_keywords,
                    max_chars=rules.form8k.ex99_max_excerpt_chars,
                )
                ex99_descriptions[fname] = fdesc or "6-K fallback PDF exhibit"
                if fname not in known_names:
                    known_names.add(fname)
                    exhibits.append(
                        ParsedExhibitMeta(
                            filename=fname,
                            exhibit_type="6-K-PDF",
                            attachment_role="ex99_press_release",
                            sec_url=furl,
                            description=fdesc,
                        )
                    )

    return Raw6kParsed(
        cik=row.cik,
        accession=row.accession,
        form=row.form,
        filed_at=row.filed_at.isoformat(),
        event_date=row.period_of_report.isoformat() if row.period_of_report else None,
        primary_document=row.primary_document,
        description=row.description,
        sec_url=row.primary_document_url,
        primary_text=cleaned_primary,
        primary_sha256=p_sha,
        ex99_texts=ex99_texts,
        ex99_excerpts=ex99_excerpts,
        ex99_descriptions=ex99_descriptions,
        exhibits=exhibits,
    )


def apply_6k_rules(
    raw: Raw6kParsed,
    rules: SecEventsRules,
    *,
    symbol: str | None = None,
    company_name: str | None = None,
    is_context_only: bool = False,
) -> tuple[RawEventCandidate | None, SuppressedEventRecord | None, dict[str, str]]:
    """Apply externalized 6-K earnings routing and routine noise patterns (0 SEC requests)."""
    filed_d = date.fromisoformat(raw.filed_at)
    event_d = date.fromisoformat(raw.event_date) if raw.event_date else None

    opening_parts = [raw.description or "", raw.primary_text[:2500]]
    for fname, txt in raw.ex99_texts.items():
        opening_parts.append(raw.ex99_descriptions.get(fname) or "")
        opening_parts.append(txt[:2500])
    classification_text = "\n".join(p for p in opening_parts if p)

    suppression_reason = classify_6k_text(classification_text, rules)
    if suppression_reason is not None:
        suppressed = SuppressedEventRecord(
            accession=raw.accession,
            form=raw.form,
            filed_at=filed_d,
            category="foreign_6k",
            suppression_reason=suppression_reason,
            items=[],
            summary_hint=f"{raw.form} ({raw.description or raw.primary_document or raw.accession}): {suppression_reason}",
        )
        return None, suppressed, {}

    nodash = raw.accession.replace("-", "")
    source_id = f"sec_event:{raw.cik}:{nodash}:6k"
    source_texts: dict[str, str] = {}
    excerpt_parts: list[str] = []
    if raw.primary_text:
        excerpt_parts.append(raw.primary_text[: rules.form8k.primary_max_excerpt_chars])
        source_texts[source_id] = raw.primary_text

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

    lag_days = (filed_d - event_d).days if event_d is not None else None
    thread_key = f"6k:{raw.cik}:{raw.event_date or raw.filed_at}:{nodash}"

    facts: dict[str, Any] = {
        "is_fpi_6k": True,
        "fpi_exempt_from_form4": True,
        "exhibit_source_ids": exhibit_source_ids,
        "lazy_attachment_count": len(lazy_refs),
        "description": raw.description,
        "is_amendment": raw.form.endswith("/A"),
    }

    cand = RawEventCandidate(
        candidate_id=deterministic_signal_id("6k", raw.cik, raw.accession),
        cik=raw.cik,
        symbol=symbol,
        company_name=company_name,
        category="foreign_6k",
        form=raw.form,
        accession=raw.accession,
        amends_accession=None,
        event_thread_key=thread_key,
        filed_at=filed_d,
        event_date=event_d,
        reporting_lag_days=lag_days,
        items=[],
        statutory_critical_items=[],
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


__all__ = [
    "Raw6kParsed",
    "apply_6k_rules",
    "classify_6k_text",
    "parse_6k_raw",
]
