"""Deterministic Schedule 13D / 13G parser (legacy SC 13D/G & 2024-12-18+ SCHEDULE 13D/G).

Handles:
1. Both legacy ``SC 13D / SC 13D/A / SC 13G / SC 13G/A`` and modern
   ``SCHEDULE 13D / SCHEDULE 13D/A / SCHEDULE 13G / SCHEDULE 13G/A`` form names.
2. Stripping ``xslSCHEDULE_13D_X01/`` & ``xslSCHEDULE_13G_X01/`` (``xslSCHEDULE_13[DG]_X\\d+/``)
   XSLT prefixes to fetch structured XML directly, with conservative HTML/TXT fallback.
3. ``13G -> 13D`` conversion detection and Item 6 cash-settled derivative / TRS extraction.
4. Combined ``typeOfReportingPerson`` (``IA/IC/BK``) + delta (< 1.0 pp) + passive institution list filtering.
5. Strictly single-CIK EFTS backfill (policy ``filing_search_cik``) when issuer submissions omit third-party 13D/Gs.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import re
from threading import Event
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import quote, urlencode
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from ....research_signals import RawEventCandidate, SuppressedEventRecord, deterministic_signal_id
from ...policy import Denied, FinDataPolicy, load_policy
from ..base import StreamTransport, Transport
from ..sec_deep import _DTD_OR_ENTITY_RE, _parse_date, normalize_accession, strip_html_preserving_tables
from ..sec_edgar import EFTS_URL, PROVIDER, cik10
from .pacing import PacingTelemetry, RawSubmissionRow, SecPacingError, SecPacingLimiter, sec_get_json, sec_get_text
from .rules import SecEventsRules

_XSL_13DG_RE = re.compile(r"^(?:xsl(?:SCHEDULE_13[DG]_X\d+|[^/]+)/)+", re.IGNORECASE)
_URL_XSL_13DG_RE = re.compile(r"/xsl(?:SCHEDULE_13[DG]_X\d+|[^/]+)/", re.IGNORECASE)
_HTML_LIKELY_RE = re.compile(r"<(?:html|body|div|table|p|span)\b", re.IGNORECASE)
_DERIVATIVE_KW_RE = re.compile(
    r"\b(?:cash[- ]settled|total\s+return\s+swap|trs\b|swap\s+agreement|derivative|forward\s+contract|collar|option\s+agreement|synthetic\s+long)\b",
    re.IGNORECASE,
)
_G_TO_D_RE = re.compile(
    r"(?:previously|formerly)\s+(?:reported|filed)\s+(?:beneficial\s+ownership\s+)?on\s+Schedule\s+13G|"
    r"convert(?:ed|ing)\s+from\s+(?:a\s+)?Schedule\s+13G",
    re.IGNORECASE,
)


class ReportingPerson13DG(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    cik: str | None = None
    reporting_person_types: list[str] = Field(default_factory=list)
    shares_beneficially_owned: str | None = None
    percent_of_class: str | None = None


class Raw13dgParsed(BaseModel):
    """Unfiltered deterministic 13D/13G parse saved to ``sec_events_l1_raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cik: str
    accession: str
    form: str
    form_family: Literal["13D", "13G"]
    is_amendment: bool = False
    is_modern_schedule_form: bool = False
    filed_at: str
    event_date: str | None = None
    sec_url: str
    filename: str | None = None
    content_sha256: str | None = None
    parse_format: Literal["xml", "html_fallback", "text_fallback"] = "xml"
    reporting_persons: list[ReportingPerson13DG] = Field(default_factory=list)
    primary_filer_name: str = "Unknown Filer"
    primary_filer_key: str = "unknown_filer"
    reporting_person_types: list[str] = Field(default_factory=list)
    aggregate_shares: str | None = None
    percent_of_class: str | None = None
    item4_text: str = ""
    item6_text: str = ""
    has_cash_settled_derivatives_or_trs: bool = False
    explicit_13g_to_13d_conversion: bool = False
    explicit_amends_accession: str | None = None
    excerpt: str = ""


def strip_13dg_xsl_prefix(doc_path: str | None) -> str | None:
    if not doc_path:
        return None
    cleaned = _XSL_13DG_RE.sub("", doc_path.strip())
    return cleaned or None


def strip_13dg_xsl_from_url(url: str) -> str:
    return _URL_XSL_13DG_RE.sub("/", url.strip())


def is_13dg_form(form: str | None, rules: SecEventsRules) -> bool:
    norm = (form or "").strip().upper()
    return norm in set(rules.form13dg.supported_forms)


def _normalize_form_family(form: str) -> tuple[Literal["13D", "13G"], bool, bool]:
    u = form.strip().upper()
    is_amend = u.endswith("/A")
    is_modern = u.startswith("SCHEDULE ")
    family: Literal["13D", "13G"] = "13D" if "13D" in u else "13G"
    return family, is_amend, is_modern


def _normalize_filer_key(name: str, cik: str | None) -> str:
    if cik and cik.isdigit():
        return cik10(cik)
    cleaned = re.sub(r"\b(?:llc|inc|corp|corporation|lp|l\.p\.|ltd|co|plc|group|holdings?)\b", "", name.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", cleaned).strip("_")
    return slug[:64] or "unknown_filer"


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_first_text(root: ET.Element, *tag_names: str) -> str | None:
    wanted = set(tag_names)
    for node in root.iter():
        if _local_tag(node.tag) in wanted:
            txt = "".join(node.itertext()).strip()
            if txt:
                return txt
    return None


def _find_all_texts(root: ET.Element, *tag_names: str) -> list[str]:
    wanted = set(tag_names)
    out: list[str] = []
    for node in root.iter():
        if _local_tag(node.tag) in wanted:
            txt = "".join(node.itertext()).strip()
            if txt:
                out.append(txt)
    return out


def _parse_decimal_clean(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    cleaned = re.sub(r"[,%\s$]", "", raw.strip())
    m = re.search(r"-?\d+(?:\.\d+)?", cleaned)
    if not m:
        return None
    try:
        dec = Decimal(m.group(0))
        return dec if dec.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _parse_13dg_xml(
    xml_text: str,
    *,
    cik: str,
    accession: str,
    form: str,
    filed_at: date,
    event_date: date | None,
    sec_url: str,
    filename: str | None,
    content_sha256: str | None,
) -> Raw13dgParsed:
    if _DTD_OR_ENTITY_RE.search(xml_text[:4096]):
        raise ValueError(f"Unsafe DTD/ENTITY in 13D/G XML {accession}")
    root = ET.fromstring(xml_text.strip())
    family, is_amend, is_modern = _normalize_form_family(form)

    person_nodes = [
        node
        for node in root.iter()
        if _local_tag(node.tag) in ("reportingPersonInfo", "reportingPerson", "coverPageHeaderReportingPersonDetails")
    ]
    persons: list[ReportingPerson13DG] = []
    for pnode in person_nodes:
        pname = _find_first_text(
            pnode,
            "nameOfReportingPerson",
            "reportingPersonName",
            "entityName",
            "name",
        )
        if not pname:
            continue
        pcik_raw = _find_first_text(pnode, "reportingPersonCik", "cik", "reportingPersonCIK")
        pcik = cik10(pcik_raw) if (pcik_raw and pcik_raw.strip().isdigit() and int(pcik_raw) > 0) else None
        raw_types = _find_all_texts(pnode, "typeOfReportingPerson", "reportingPersonType")
        ptypes: list[str] = []
        for rt in raw_types:
            for tok in re.split(r"[,;\s/]+", rt.upper()):
                if tok and re.fullmatch(r"[A-Z]{2}", tok) and tok not in ptypes:
                    ptypes.append(tok)
        shares_raw = _find_first_text(
            pnode,
            "aggregateAmountOwned",
            "amountBeneficiallyOwned",
            "sharesBeneficiallyOwned",
            "aggregateAmountBeneficiallyOwned",
        )
        pct_raw = _find_first_text(pnode, "percentOfClass", "classPercent", "percentOwned")
        shares_dec = _parse_decimal_clean(shares_raw)
        pct_dec = _parse_decimal_clean(pct_raw)
        persons.append(
            ReportingPerson13DG(
                name=pname.strip(),
                cik=pcik,
                reporting_person_types=ptypes,
                shares_beneficially_owned=format(shares_dec, "f") if shares_dec is not None else None,
                percent_of_class=format(pct_dec, "f") if pct_dec is not None else None,
            )
        )

    if not persons:
        fallback_name = _find_first_text(root, "nameOfReportingPerson", "reportingPersonName", "filerName") or "Unknown Filer"
        fallback_shares = _parse_decimal_clean(_find_first_text(root, "aggregateAmountOwned", "amountBeneficiallyOwned"))
        fallback_pct = _parse_decimal_clean(_find_first_text(root, "percentOfClass", "classPercent"))
        fallback_types = [
            tok
            for rt in _find_all_texts(root, "typeOfReportingPerson")
            for tok in re.split(r"[,;\s/]+", rt.upper())
            if tok and re.fullmatch(r"[A-Z]{2}", tok)
        ]
        persons.append(
            ReportingPerson13DG(
                name=fallback_name,
                reporting_person_types=list(dict.fromkeys(fallback_types)),
                shares_beneficially_owned=format(fallback_shares, "f") if fallback_shares is not None else None,
                percent_of_class=format(fallback_pct, "f") if fallback_pct is not None else None,
            )
        )

    primary = persons[0]
    all_types = list(dict.fromkeys(t for p in persons for t in p.reporting_person_types))
    # Prefer largest percent_of_class if multiple affiliated entities report group total
    pct_vals = [Decimal(p.percent_of_class) for p in persons if p.percent_of_class is not None]
    agg_pct = format(max(pct_vals), "f") if pct_vals else None
    share_vals = [Decimal(p.shares_beneficially_owned) for p in persons if p.shares_beneficially_owned is not None]
    agg_shares = format(max(share_vals), "f") if share_vals else None

    item4 = "\n".join(_find_all_texts(root, "item4", "purposeOfTransaction", "purposeOfTransactionText"))
    item6 = "\n".join(_find_all_texts(root, "item6", "contractsArrangements", "contractsEtc"))
    ev_date_str = _find_first_text(root, "dateOfEvent", "eventDate")
    ev_date = _parse_date(ev_date_str) or event_date
    amends_acc = _find_first_text(root, "amendsAccession")

    combined_text = f"{item4}\n{item6}"
    has_deriv = bool(_DERIVATIVE_KW_RE.search(item6 or combined_text))
    explicit_conv = family == "13D" and bool(_G_TO_D_RE.search(combined_text))

    excerpt = _build_13dg_excerpt(
        form=form,
        accession=accession,
        filed_at=filed_at.isoformat(),
        persons=persons,
        agg_shares=agg_shares,
        agg_pct=agg_pct,
        all_types=all_types,
        item4=item4,
        item6=item6,
        has_deriv=has_deriv,
    )
    return Raw13dgParsed(
        cik=cik10(cik),
        accession=accession,
        form=form,
        form_family=family,
        is_amendment=is_amend,
        is_modern_schedule_form=is_modern,
        filed_at=filed_at.isoformat(),
        event_date=ev_date.isoformat() if ev_date else None,
        sec_url=sec_url,
        filename=filename,
        content_sha256=content_sha256,
        parse_format="xml",
        reporting_persons=persons,
        primary_filer_name=primary.name,
        primary_filer_key=_normalize_filer_key(primary.name, primary.cik),
        reporting_person_types=all_types,
        aggregate_shares=agg_shares,
        percent_of_class=agg_pct,
        item4_text=item4,
        item6_text=item6,
        has_cash_settled_derivatives_or_trs=has_deriv,
        explicit_13g_to_13d_conversion=explicit_conv,
        explicit_amends_accession=amends_acc,
        excerpt=excerpt,
    )


def _parse_13dg_html_or_text(
    raw_text: str,
    *,
    is_html: bool,
    cik: str,
    accession: str,
    form: str,
    filed_at: date,
    event_date: date | None,
    sec_url: str,
    filename: str | None,
    content_sha256: str | None,
) -> Raw13dgParsed:
    clean = strip_html_preserving_tables(raw_text)[0] if is_html else raw_text
    family, is_amend, is_modern = _normalize_form_family(form)

    # Conservative cover extraction
    name_m = re.search(
        r"(?:NAMES?\s+OF\s+REPORTING\s+PERSONS?)\b[^\n]*\n+\s*([^\n|]{2,140})",
        clean,
        re.IGNORECASE,
    )
    filer_name = name_m.group(1).strip() if name_m else "Unknown Filer"

    types_m = re.search(
        r"TYPE\s+OF\s+REPORTING\s+PERSON[^\n]*\n+\s*([A-Z]{2}(?:[\s,/]+[A-Z]{2})*)",
        clean,
        re.IGNORECASE,
    )
    ptypes = (
        [t for t in re.split(r"[\s,/]+", types_m.group(1).upper()) if re.fullmatch(r"[A-Z]{2}", t)]
        if types_m
        else []
    )

    shares_m = re.search(
        r"AGGREGATE\s+AMOUNT\s+BENEFICIALLY\s+OWNED[^\n]*\n+\s*([\d,]+(?:\.\d+)?)",
        clean,
        re.IGNORECASE,
    )
    agg_shares = format(_parse_decimal_clean(shares_m.group(1)), "f") if (shares_m and _parse_decimal_clean(shares_m.group(1)) is not None) else None

    pct_m = re.search(
        r"PERCENT\s+OF\s+CLASS\s+REPRESENTED[^\n]*\n+\s*([\d,]+(?:\.\d+)?)\s*%?",
        clean,
        re.IGNORECASE,
    )
    agg_pct = format(_parse_decimal_clean(pct_m.group(1)), "f") if (pct_m and _parse_decimal_clean(pct_m.group(1)) is not None) else None

    item4 = _slice_13dg_item(clean, "4", next_items=("5", "6", "7"))
    item6 = _slice_13dg_item(clean, "6", next_items=("7", "SIGNATURE"))
    has_deriv = bool(_DERIVATIVE_KW_RE.search(item6 or clean[:30_000]))
    explicit_conv = family == "13D" and bool(_G_TO_D_RE.search(clean[:30_000]))

    person = ReportingPerson13DG(
        name=filer_name,
        reporting_person_types=ptypes,
        shares_beneficially_owned=agg_shares,
        percent_of_class=agg_pct,
    )
    excerpt = _build_13dg_excerpt(
        form=form,
        accession=accession,
        filed_at=filed_at.isoformat(),
        persons=[person],
        agg_shares=agg_shares,
        agg_pct=agg_pct,
        all_types=ptypes,
        item4=item4,
        item6=item6,
        has_deriv=has_deriv,
        fallback_body=clean[:8000] if not (item4 or item6) else "",
    )

    return Raw13dgParsed(
        cik=cik10(cik),
        accession=accession,
        form=form,
        form_family=family,
        is_amendment=is_amend,
        is_modern_schedule_form=is_modern,
        filed_at=filed_at.isoformat(),
        event_date=event_date.isoformat() if event_date else None,
        sec_url=sec_url,
        filename=filename,
        content_sha256=content_sha256,
        parse_format="html_fallback" if is_html else "text_fallback",
        reporting_persons=[person],
        primary_filer_name=filer_name,
        primary_filer_key=_normalize_filer_key(filer_name, None),
        reporting_person_types=ptypes,
        aggregate_shares=agg_shares,
        percent_of_class=agg_pct,
        item4_text=item4,
        item6_text=item6,
        has_cash_settled_derivatives_or_trs=has_deriv,
        explicit_13g_to_13d_conversion=explicit_conv,
        excerpt=excerpt,
    )


def _slice_13dg_item(text: str, item_num: str, *, next_items: tuple[str, ...]) -> str:
    start_re = re.compile(rf"(?im)^\s*Item\s+{re.escape(item_num)}\b[^\n]*")
    m = start_re.search(text)
    if not m:
        return ""
    rest = text[m.start() :]
    end_idx = len(rest)
    for nxt in next_items:
        if nxt == "SIGNATURE":
            nm = re.search(r"(?im)^\s*SIGNATURES?\s*$", rest[10:])
        else:
            nm = re.search(rf"(?im)^\s*Item\s+{re.escape(nxt)}\b", rest[10:])
        if nm:
            end_idx = min(end_idx, 10 + nm.start())
    return rest[:end_idx].strip()[:8000]


def _build_13dg_excerpt(
    *,
    form: str,
    accession: str,
    filed_at: str,
    persons: Sequence[ReportingPerson13DG],
    agg_shares: str | None,
    agg_pct: str | None,
    all_types: Sequence[str],
    item4: str,
    item6: str,
    has_deriv: bool,
    fallback_body: str = "",
) -> str:
    lines = [
        f"{form} ({accession}) filed {filed_at}",
        f"Reporting Person(s): {', '.join(p.name for p in persons)}",
        f"Type(s): {', '.join(all_types) or 'unknown'} | Shares: {agg_shares or 'unknown'} | Percent of Class: {agg_pct or 'unknown'}%",
    ]
    if has_deriv:
        lines.append("Item 6 Signal: Mentions cash-settled derivatives / total return swaps / contracts.")
    if item4:
        lines.append(f"Item 4 (Purpose of Transaction):\n{item4[:5000]}")
    if item6:
        lines.append(f"Item 6 (Contracts/Arrangements):\n{item6[:3000]}")
    if fallback_body:
        lines.append(fallback_body)
    return "\n\n".join(lines).strip()


def parse_13dg_raw(
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
) -> Raw13dgParsed:
    """Fetch & deterministically parse legacy ``SC 13D/G`` or modern ``SCHEDULE 13D/G``."""
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(row.cik)}/{row.nodash}"
    clean_doc = strip_13dg_xsl_prefix(row.primary_document)
    if clean_doc:
        target_url = f"{base_url}/{clean_doc}"
    elif row.primary_document_url:
        target_url = strip_13dg_xsl_from_url(row.primary_document_url)
    else:
        raise ValueError(f"13D/G {row.accession} has no primaryDocument URL.")

    raw_text, ct, digest = sec_get_text(
        target_url,
        category="ownership",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        accept="application/xml, text/xml, text/html, text/plain",
        max_bytes=10_000_000,
        large=True,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    trimmed = raw_text.lstrip()
    looks_xml = (clean_doc or "").lower().endswith(".xml") or trimmed.startswith("<?xml") or (
        trimmed.startswith("<edgarSubmission") or trimmed.startswith("<schedule13")
    )
    if looks_xml and not _HTML_LIKELY_RE.search(trimmed[:512]):
        try:
            return _parse_13dg_xml(
                raw_text,
                cik=row.cik,
                accession=row.accession,
                form=row.form,
                filed_at=row.filed_at,
                event_date=row.period_of_report,
                sec_url=target_url,
                filename=clean_doc,
                content_sha256=digest,
            )
        except ET.ParseError:
            pass

    is_html = "html" in ct.lower() or bool(_HTML_LIKELY_RE.search(raw_text[:4096]))
    return _parse_13dg_html_or_text(
        raw_text,
        is_html=is_html,
        cik=row.cik,
        accession=row.accession,
        form=row.form,
        filed_at=row.filed_at,
        event_date=row.period_of_report,
        sec_url=target_url,
        filename=clean_doc,
        content_sha256=digest,
    )


def backfill_13dg_via_cik_efts(
    cik: str,
    *,
    start_date: date,
    as_of: date,
    existing_accessions: set[str],
    rules: SecEventsRules,
    user_agent: str | None,
    limiter: SecPacingLimiter | None,
    policy: FinDataPolicy | None,
    transport: Transport | None,
    stream_transport: StreamTransport | None,
    cancel_event: Event | None,
    deadline: float | None,
    telemetry: PacingTelemetry | None,
) -> list[RawSubmissionRow]:
    """Strictly single-CIK EFTS backfill for third-party 13D/G filings omitted from issuer submissions."""
    target_cik = cik10(cik)
    active_policy = policy or load_policy()
    cat = "filing_search_cik"
    if isinstance(active_policy.decide(PROVIDER, cat), Denied):
        if not isinstance(active_policy.decide(PROVIDER, "filing_search"), Denied):
            cat = "filing_search"
        else:
            return []

    params = {
        "q": "*",
        "ciks": target_cik,
        "forms": ",".join(rules.form13dg.supported_forms),
        "dateRange": "custom",
        "startdt": start_date.isoformat(),
        "enddt": as_of.isoformat(),
    }
    url = f"{EFTS_URL}?{urlencode(params, quote_via=quote)}"
    try:
        doc, _sha = sec_get_json(
            url,
            category=cat,
            user_agent=user_agent,
            limiter=limiter,
            policy=active_policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
        )
    except SecPacingError as err:
        if err.reason in ("cancelled", "deadline_exceeded"):
            raise
        return []

    hits = ((doc.get("hits") or {}).get("hits")) if isinstance(doc, Mapping) else None
    if not isinstance(hits, list):
        return []

    out: list[RawSubmissionRow] = []
    seen = set(existing_accessions)
    for h in hits:
        src = h.get("_source") if isinstance(h, Mapping) else None
        if not isinstance(src, Mapping):
            continue
        hit_ciks: list[str] = []
        for raw_c in src.get("ciks") or []:
            try:
                hit_ciks.append(cik10(raw_c))
            except ValueError:
                continue
        if target_cik not in hit_ciks:
            continue
        raw_acc = src.get("adsh")
        filed = _parse_date(src.get("file_date"))
        form = str(src.get("form") or "").strip().upper()
        if not isinstance(raw_acc, str) or filed is None or filed > as_of or not is_13dg_form(form, rules):
            continue
        try:
            dashed, nodash = normalize_accession(raw_acc)
        except ValueError:
            continue
        if dashed in seen:
            continue
        seen.add(dashed)
        doc_name = str(h.get("_id", "")).partition(":")[2] or None
        clean_doc = strip_13dg_xsl_prefix(doc_name)
        base_url = f"https://www.sec.gov/Archives/edgar/data/{int(target_cik)}/{nodash}"
        out.append(
            RawSubmissionRow(
                cik=target_cik,
                accession=dashed,
                nodash=nodash,
                form=form,
                filed_at=filed,
                period_of_report=_parse_date(src.get("period_ending")),
                primary_document=clean_doc,
                primary_document_url=f"{base_url}/{clean_doc}" if clean_doc else None,
                index_url=f"{base_url}/{dashed}-index.htm",
                description=src.get("file_description") if isinstance(src.get("file_description"), str) else None,
                items=None,
            )
        )
    return out


def _matches_passive_whitelist(filer_name: str, rules: SecEventsRules) -> bool:
    low = filer_name.casefold()
    return any(pat.casefold() in low for pat in rules.form13dg.passive_institution_whitelist if pat)


def process_13dg_batch(
    raw_filings: Sequence[Raw13dgParsed],
    rules: SecEventsRules,
    *,
    target_start: date,
    context_start: date,
    as_of: date,
    is_fpi: bool = False,
    symbol: str | None = None,
    company_name: str | None = None,
) -> tuple[list[RawEventCandidate], list[SuppressedEventRecord], dict[str, str]]:
    """Detect ``13G -> 13D`` conversions, compute stake deltas, and filter passive 13G noise."""
    ordered = sorted(
        [r for r in raw_filings if date.fromisoformat(r.filed_at) <= as_of],
        key=lambda r: (r.filed_at, r.accession),
    )

    last_by_filer: dict[str, Raw13dgParsed] = {}
    candidates: list[RawEventCandidate] = []
    suppressed: list[SuppressedEventRecord] = []
    source_texts: dict[str, str] = {}

    passive_types = set(rules.form13dg.passive_reporting_person_types)
    delta_threshold = rules.form13dg.passive_delta_threshold_pp
    initial_suppress_below = rules.form13dg.passive_initial_13g_suppress_below_pct

    for raw in ordered:
        filed_d = date.fromisoformat(raw.filed_at)
        prior = last_by_filer.get(raw.primary_filer_key)
        last_by_filer[raw.primary_filer_key] = raw

        prior_pct = Decimal(prior.percent_of_class) if (prior and prior.percent_of_class is not None) else None
        cur_pct = Decimal(raw.percent_of_class) if raw.percent_of_class is not None else None
        delta_pp = (
            (cur_pct - prior_pct).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if (cur_pct is not None and prior_pct is not None)
            else None
        )
        is_conversion = raw.explicit_13g_to_13d_conversion or (
            prior is not None and prior.form_family == "13G" and raw.form_family == "13D"
        )
        amends_acc = raw.explicit_amends_accession or (prior.accession if (prior is not None and (raw.is_amendment or is_conversion)) else None)

        if filed_d < context_start:
            continue
        is_ctx_only = filed_d < target_start

        is_passive_inst = _matches_passive_whitelist(raw.primary_filer_name, rules)
        types_set = set(raw.reporting_person_types)
        has_passive_type = bool(types_set & passive_types) and types_set.issubset(passive_types | {"OO", "CO", "HC", "FI", "EP"})

        suppress_reason: str | None = None
        if raw.form_family == "13G" and not is_conversion:
            if delta_pp is not None and abs(delta_pp) < delta_threshold and (is_passive_inst or has_passive_type):
                suppress_reason = "passive_13g_minor_change"
            elif not raw.is_amendment and is_passive_inst and (has_passive_type or not types_set):
                if cur_pct is None or cur_pct < initial_suppress_below:
                    suppress_reason = "passive_13g_routine_threshold"

        if suppress_reason is not None:
            if not is_ctx_only:
                suppressed.append(
                    SuppressedEventRecord(
                        accession=raw.accession,
                        form=raw.form,
                        filed_at=filed_d,
                        category="ownership_13dg",
                        suppression_reason=suppress_reason,
                        items=list(raw.reporting_person_types),
                        summary_hint=(
                            f"{raw.form} by {raw.primary_filer_name} ({raw.percent_of_class or '?'}%): {suppress_reason}"
                        ),
                    )
                )
            continue

        nodash = raw.accession.replace("-", "")
        source_id = f"sec_event:{raw.cik}:{nodash}:13dg"
        event_d = date.fromisoformat(raw.event_date) if raw.event_date else None
        lag_days = (filed_d - event_d).days if event_d is not None else None
        thread_key = f"13dg:{raw.cik}:{raw.primary_filer_key}"

        facts: dict[str, Any] = {
            "form_family": raw.form_family,
            "is_amendment": raw.is_amendment,
            "is_modern_schedule_form": raw.is_modern_schedule_form,
            "is_13g_to_13d_conversion": is_conversion,
            "primary_filer_name": raw.primary_filer_name,
            "reporting_persons": [p.model_dump(mode="json") for p in raw.reporting_persons],
            "reporting_person_types": raw.reporting_person_types,
            "is_passive_institution": is_passive_inst,
            "aggregate_shares": raw.aggregate_shares,
            "percent_of_class": raw.percent_of_class,
            "prior_percent_of_class": format(prior_pct, "f") if prior_pct is not None else None,
            "delta_percentage_points": format(delta_pp, "f") if delta_pp is not None else None,
            "has_cash_settled_derivatives_or_trs": raw.has_cash_settled_derivatives_or_trs,
            "share_unit_basis": "ordinary_vs_adr_unverified" if is_fpi else "common_shares",
        }
        source_texts[source_id] = raw.excerpt

        candidates.append(
            RawEventCandidate(
                candidate_id=deterministic_signal_id("13dg", raw.cik, raw.accession),
                cik=raw.cik,
                symbol=symbol,
                company_name=company_name,
                category="ownership_13dg",
                form=raw.form,
                accession=raw.accession,
                amends_accession=amends_acc,
                event_thread_key=thread_key,
                filed_at=filed_d,
                event_date=event_d,
                reporting_lag_days=lag_days,
                items=list(raw.reporting_person_types),
                statutory_critical_items=["13G_TO_13D_CONVERSION"] if is_conversion else [],
                facts=facts,
                excerpt=raw.excerpt,
                source_id=source_id,
                sec_url=raw.sec_url,
                filename=raw.filename,
                content_sha256=raw.content_sha256 or sha256(raw.excerpt.encode("utf-8")).hexdigest(),
                lazy_attachments=[],
                is_context_only=is_ctx_only,
            )
        )

    return candidates, suppressed, source_texts


__all__ = [
    "Raw13dgParsed",
    "ReportingPerson13DG",
    "backfill_13dg_via_cik_efts",
    "is_13dg_form",
    "parse_13dg_raw",
    "process_13dg_batch",
    "strip_13dg_xsl_from_url",
    "strip_13dg_xsl_prefix",
]
