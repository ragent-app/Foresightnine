"""Deterministic Form 4 / 4/A XML parser with pitfall hardening.

Handles all 7 Form 4 failure modes:
1. Strips XSLT prefix (e.g. ``xslF345X05/``) to fetch raw ``.xml`` directly.
2. Parses 1-to-N ``<reportingOwner>`` elements and normalizes ``officer_tier``.
3. Detects ``4/A`` amendments (``amends_accession`` / ``superseded_by_accession``) to avoid double counting.
4. Links every ``<footnoteId>`` back to full ``<footnote>`` text on each transaction.
5. Separates Table I (non-derivative) and Table II (derivative), and splits full & partial ``M + S`` cashless exercises.
6. Dual ``10b5-1`` detection via ``<aff10b5One>`` XML flag + negative-aware footnote regexes.
7. Never coerces missing ``pricePerShare`` to 0: sets ``price_status="missing_or_footnote_only"`` and
   exempts from the ``$25,000`` dust floor; aggregates same-day split fills per
   ``(owner_cik, tx_date, code, is_10b5_1, direct_or_indirect)`` before dust filtering;
   computes ``pre_shares = post_shares + shares_sold`` safely (100% exit boundary).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import re
from threading import Event
from typing import Any, Literal, Mapping, Sequence
import xml.etree.ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field

from ....research_signals import RawEventCandidate, SuppressedEventRecord, deterministic_signal_id
from ...policy import FinDataPolicy
from ..base import StreamTransport, Transport
from ..sec_deep import _DTD_OR_ENTITY_RE, _SAFE_FILENAME_RE
from ..sec_edgar import cik10
from .pacing import PacingTelemetry, RawSubmissionRow, SecPacingLimiter, sec_get_json, sec_get_text
from .rules import OfficerTier, SecEventsRules

_XSL_PREFIX_RE = re.compile(r"^(?:xsl[^/]+/)+", re.IGNORECASE)
_URL_XSL_RE = re.compile(r"/xsl[^/]+/", re.IGNORECASE)
_ACCESSION_IN_TEXT_RE = re.compile(r"\b(\d{10}-\d{2}-\d{6})\b")

_TIER_RANK: dict[str, int] = {
    "ceo_cfo_chair": 0,
    "c_suite_evp": 1,
    "vp_other_officer": 2,
    "director": 3,
    "ten_pct_owner": 4,
}


class ReportingOwnerInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    owner_cik: str
    owner_name: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    is_other: bool = False
    officer_title: str | None = None
    other_text: str | None = None
    officer_tier: OfficerTier = "director"


class ParsedForm4Transaction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    table: Literal["I", "II"]
    security_title: str
    transaction_date: str | None = None
    transaction_code: str
    equity_swap_involved: bool = False
    shares: str
    price_per_share: str | None = None
    price_status: Literal["explicit", "missing_or_footnote_only"] = "explicit"
    acquired_disposed_code: str | None = None
    post_shares: str | None = None
    direct_or_indirect: Literal["D", "I"] = "D"
    nature_of_ownership: str | None = None
    is_10b5_1: bool = False
    plan_10b5_1_source: Literal["xml_flag", "footnote", "both", "none"] = "none"
    footnote_ids: list[str] = Field(default_factory=list)
    footnotes: list[dict[str, str]] = Field(default_factory=list)


class RawForm4Parsed(BaseModel):
    """Unfiltered deterministic Form 4 / 4/A XML parse saved to ``sec_events_l1_raw``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    cik: str
    accession: str
    form: str
    filed_at: str
    period_of_report: str | None = None
    date_of_original_submission: str | None = None
    is_amendment: bool = False
    explicit_amends_accession: str | None = None
    sec_url: str
    filename: str | None = None
    content_sha256: str | None = None
    document_aff_10b5_1: bool = False
    issuer_cik: str | None = None
    issuer_name: str | None = None
    issuer_symbol: str | None = None
    reporting_owners: list[ReportingOwnerInfo] = Field(default_factory=list)
    footnotes: dict[str, str] = Field(default_factory=dict)
    table_1_transactions: list[ParsedForm4Transaction] = Field(default_factory=list)
    table_2_transactions: list[ParsedForm4Transaction] = Field(default_factory=list)
    raw_xml_excerpt: str = ""


class AggregatedTradeGroup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    owner_cik: str
    owner_name: str
    officer_tier: OfficerTier
    transaction_date: str
    transaction_code: Literal["P", "S"]
    is_10b5_1: bool
    plan_10b5_1_source: Literal["xml_flag", "footnote", "both", "none"] = "none"
    direct_or_indirect: Literal["D", "I"]
    nature_of_ownership: str | None = None
    total_shares: str
    cashless_exercise_shares: str = "0"
    discretionary_net_sale_shares: str = "0"
    shares_retained_from_exercise: str = "0"
    is_cashless_exercise: bool = False
    cashless_split_kind: Literal[
        "none",
        "full",
        "partial_exercise_to_cover",
        "partial_excess_discretionary_sale",
    ] = "none"
    price_status: Literal["explicit", "missing_or_footnote_only"] = "explicit"
    weighted_avg_price_usd: str | None = None
    total_value_usd: str | None = None
    discretionary_value_usd: str | None = None
    post_shares: str | None = None
    pre_shares: str | None = None
    pct_holding_change: str | None = None
    is_100pct_exit: bool = False
    is_new_position: bool = False
    footnote_texts: list[str] = Field(default_factory=list)


def strip_xsl_prefix(path_or_filename: str | None) -> str | None:
    if not path_or_filename:
        return None
    cleaned = _XSL_PREFIX_RE.sub("", path_or_filename.strip())
    return cleaned or None


def strip_xsl_from_url(url: str) -> str:
    return _URL_XSL_RE.sub("/", url.strip())


def _local_tag(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _child_by_local(elem: ET.Element, name: str) -> ET.Element | None:
    for child in elem:
        if _local_tag(child.tag) == name:
            return child
    return None


def _children_by_local(elem: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in elem if _local_tag(child.tag) == name]


def _text_of(elem: ET.Element | None, *path: str) -> str | None:
    cur = elem
    for seg in path:
        if cur is None:
            return None
        cur = _child_by_local(cur, seg)
    if cur is None or cur.text is None:
        return None
    txt = cur.text.strip()
    return txt or None


def _value_of(elem: ET.Element | None, *path: str) -> str | None:
    cur = elem
    for seg in path:
        if cur is None:
            return None
        cur = _child_by_local(cur, seg)
    if cur is None:
        return None
    val_node = _child_by_local(cur, "value")
    if val_node is not None and val_node.text and val_node.text.strip():
        return val_node.text.strip()
    if cur.text and cur.text.strip():
        return cur.text.strip()
    return None


def _is_truthy_xml(val: str | None) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "y"}


def _parse_decimal_opt(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    cleaned = raw.strip().replace(",", "").lstrip("$")
    try:
        dec = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    return dec


def normalize_officer_tier(
    *,
    is_officer: bool,
    is_director: bool,
    is_ten_percent_owner: bool,
    officer_title: str | None,
    rules: SecEventsRules,
) -> OfficerTier:
    title = (officer_title or "").strip()
    if (is_officer or title) and title:
        for tier_name in ("ceo_cfo_chair", "c_suite_evp", "vp_other_officer"):
            for pat in rules.form4.officer_title_tiers.get(tier_name, ()):
                if re.search(pat, title, re.IGNORECASE):
                    return tier_name  # type: ignore[return-value]
        return "vp_other_officer"
    if is_officer:
        return "vp_other_officer"
    if is_director:
        return "director"
    if is_ten_percent_owner:
        return "ten_pct_owner"
    return "director"


def _collect_footnote_ids(elem: ET.Element) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for node in elem.iter():
        if _local_tag(node.tag) == "footnoteId":
            fid = (node.attrib.get("id") or "").strip()
            if fid and fid not in seen:
                seen.add(fid)
                ids.append(fid)
    return ids


def _detect_10b5_1_from_footnotes(footnote_texts: Sequence[str], rules: SecEventsRules) -> bool:
    pos_pats = [re.compile(p, re.IGNORECASE) for p in rules.form4.plan_10b5_1_positive_patterns]
    neg_pats = [re.compile(p, re.IGNORECASE) for p in rules.form4.plan_10b5_1_negative_patterns]
    for fn in footnote_texts:
        if any(np.search(fn) for np in neg_pats):
            # Check sentence-by-sentence in case one sentence denies and another affirms
            sentences = re.split(r"(?<=[.!?])\s+", fn)
            for sent in sentences:
                if any(np.search(sent) for np in neg_pats):
                    continue
                if any(pp.search(sent) for pp in pos_pats):
                    return True
            continue
        if any(pp.search(fn) for pp in pos_pats):
            return True
    return False


def _resolve_form4_xml_url(
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
) -> tuple[str, str | None]:
    clean_doc = strip_xsl_prefix(row.primary_document)
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(row.cik)}/{row.nodash}"
    if clean_doc and clean_doc.lower().endswith(".xml"):
        return f"{base_url}/{clean_doc}", clean_doc

    # Fallback to index.json to locate raw XML file
    try:
        idx_doc, _sha = sec_get_json(
            f"{base_url}/index.json",
            category="ownership",
            user_agent=user_agent,
            limiter=limiter,
            policy=policy,
            transport=transport,
            stream_transport=stream_transport,
            cancel_event=cancel_event,
            deadline=deadline,
            telemetry=telemetry,
        )
        entries = ((idx_doc.get("directory") or {}).get("item")) if isinstance(idx_doc, Mapping) else None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                name = entry.get("name")
                if not isinstance(name, str) or not _SAFE_FILENAME_RE.fullmatch(name):
                    continue
                low = name.lower()
                if low.endswith(".xml") and not low.startswith(("r", "filingsummary")):
                    return f"{base_url}/{name}", name
    except Exception:
        pass

    if row.primary_document_url:
        cleaned_url = strip_xsl_from_url(row.primary_document_url)
        return cleaned_url, clean_doc
    raise ValueError(f"Form 4 {row.accession} has no XML URL.")


def parse_form4_xml_text(
    xml_text: str,
    *,
    cik: str,
    accession: str,
    form: str,
    filed_at: date,
    sec_url: str,
    filename: str | None,
    content_sha256: str | None,
    rules: SecEventsRules,
) -> RawForm4Parsed:
    """Parse raw Form 4 / 4/A XML into unfiltered ``RawForm4Parsed``."""
    raw = (xml_text or "").strip()
    if not raw or _DTD_OR_ENTITY_RE.search(raw[:4096]):
        raise ValueError(f"Unsafe or empty Form 4 XML for {accession}")

    root = ET.fromstring(raw)
    doc_type = (_text_of(root, "documentType") or form).strip().upper()
    is_amendment = form.upper().endswith("/A") or doc_type.endswith("/A")
    period_str = _text_of(root, "periodOfReport")
    orig_sub_str = _text_of(root, "dateOfOriginalSubmission")
    doc_aff_10b5_1 = _is_truthy_xml(_text_of(root, "aff10b5One"))

    issuer_node = _child_by_local(root, "issuer")
    raw_issuer_cik = _text_of(issuer_node, "issuerCik")
    try:
        issuer_cik = cik10(raw_issuer_cik) if raw_issuer_cik else cik10(cik)
    except ValueError:
        issuer_cik = cik10(cik)
    issuer_name = _text_of(issuer_node, "issuerName")
    issuer_symbol = _text_of(issuer_node, "issuerTradingSymbol")

    footnotes_map: dict[str, str] = {}
    fn_root = _child_by_local(root, "footnotes")
    if fn_root is not None:
        for fn in _children_by_local(fn_root, "footnote"):
            fid = (fn.attrib.get("id") or "").strip()
            ftext = "".join(fn.itertext()).strip()
            if fid and ftext:
                footnotes_map[fid] = ftext

    explicit_amends_acc: str | None = None
    if is_amendment:
        for ftext in footnotes_map.values():
            m = _ACCESSION_IN_TEXT_RE.search(ftext)
            if m and m.group(1) != accession:
                explicit_amends_acc = m.group(1)
                break

    owners: list[ReportingOwnerInfo] = []
    for owner_node in _children_by_local(root, "reportingOwner"):
        id_node = _child_by_local(owner_node, "reportingOwnerId")
        rel_node = _child_by_local(owner_node, "reportingOwnerRelationship")
        raw_ocik = _text_of(id_node, "rptOwnerCik")
        oname = _text_of(id_node, "rptOwnerName") or "Unknown Owner"
        try:
            ocik = cik10(raw_ocik) if raw_ocik else f"name:{re.sub(r'[^a-z0-9]+', '_', oname.lower()).strip('_')}"
        except ValueError:
            ocik = f"name:{re.sub(r'[^a-z0-9]+', '_', oname.lower()).strip('_')}"
        is_dir = _is_truthy_xml(_text_of(rel_node, "isDirector"))
        is_off = _is_truthy_xml(_text_of(rel_node, "isOfficer"))
        is_10p = _is_truthy_xml(_text_of(rel_node, "isTenPercentOwner"))
        is_oth = _is_truthy_xml(_text_of(rel_node, "isOther"))
        otitle = _text_of(rel_node, "officerTitle")
        otext = _text_of(rel_node, "otherText")
        tier = normalize_officer_tier(
            is_officer=is_off,
            is_director=is_dir,
            is_ten_percent_owner=is_10p,
            officer_title=otitle,
            rules=rules,
        )
        owners.append(
            ReportingOwnerInfo(
                owner_cik=ocik,
                owner_name=oname,
                is_director=is_dir,
                is_officer=is_off,
                is_ten_percent_owner=is_10p,
                is_other=is_oth,
                officer_title=otitle,
                other_text=otext,
                officer_tier=tier,
            )
        )

    def parse_tx_node(tx_node: ET.Element, table: Literal["I", "II"]) -> ParsedForm4Transaction | None:
        sec_title = _value_of(tx_node, "securityTitle") or ("Derivative Security" if table == "II" else "Common Stock")
        tx_date = _value_of(tx_node, "transactionDate")
        coding_node = _child_by_local(tx_node, "transactionCoding")
        tx_code = (_text_of(coding_node, "transactionCode") or "").upper()
        if not tx_code:
            return None
        equity_swap = _is_truthy_xml(_text_of(coding_node, "equitySwapInvolved"))
        tx_aff_10b5_1 = _is_truthy_xml(_text_of(coding_node, "aff10b5One")) or _is_truthy_xml(
            _text_of(tx_node, "aff10b5One")
        )

        amounts_node = _child_by_local(tx_node, "transactionAmounts")
        shares_raw = _value_of(amounts_node, "transactionShares") or _value_of(
            tx_node, "underlyingSecurity", "underlyingSecurityShares"
        )
        shares_dec = _parse_decimal_opt(shares_raw)
        if shares_dec is None or shares_dec <= 0:
            return None

        price_node = _child_by_local(amounts_node, "transactionPricePerShare") if amounts_node is not None else None
        price_val_raw = _value_of(amounts_node, "transactionPricePerShare")
        price_has_fn = price_node is not None and bool(_collect_footnote_ids(price_node))
        price_dec = _parse_decimal_opt(price_val_raw)
        # Missing or footnote-only price (or 0 with footnote on P/S) is NEVER coerced to 0
        if price_dec is None or (tx_code in ("P", "S") and price_dec <= 0 and (price_has_fn or price_val_raw is None)):
            price_str = None
            price_status: Literal["explicit", "missing_or_footnote_only"] = "missing_or_footnote_only"
        elif tx_code in ("P", "S") and price_dec <= 0:
            price_str = None
            price_status = "missing_or_footnote_only"
        else:
            price_str = format(price_dec, "f")
            price_status = "explicit"

        ad_code = (_value_of(amounts_node, "transactionAcquiredDisposedCode") or "").upper() or None
        post_raw = _value_of(tx_node, "postTransactionAmounts", "sharesOwnedFollowingTransaction")
        post_dec = _parse_decimal_opt(post_raw)
        d_or_i_raw = (_value_of(tx_node, "ownershipNature", "directOrIndirectOwnership") or "D").upper()
        d_or_i: Literal["D", "I"] = "I" if d_or_i_raw == "I" else "D"
        nature = _value_of(tx_node, "ownershipNature", "natureOfOwnership")

        fn_ids = _collect_footnote_ids(tx_node)
        linked_fns = [{"id": fid, "text": footnotes_map[fid]} for fid in fn_ids if fid in footnotes_map]
        fn_texts = [item["text"] for item in linked_fns]

        xml_10b5_1 = doc_aff_10b5_1 or tx_aff_10b5_1
        fn_10b5_1 = _detect_10b5_1_from_footnotes(fn_texts, rules)
        if xml_10b5_1 and fn_10b5_1:
            plan_src: Literal["xml_flag", "footnote", "both", "none"] = "both"
        elif xml_10b5_1:
            plan_src = "xml_flag"
        elif fn_10b5_1:
            plan_src = "footnote"
        else:
            plan_src = "none"

        return ParsedForm4Transaction(
            table=table,
            security_title=sec_title,
            transaction_date=tx_date[:10] if tx_date else None,
            transaction_code=tx_code,
            equity_swap_involved=equity_swap,
            shares=format(shares_dec, "f"),
            price_per_share=price_str,
            price_status=price_status,
            acquired_disposed_code=ad_code,
            post_shares=format(post_dec, "f") if post_dec is not None else None,
            direct_or_indirect=d_or_i,
            nature_of_ownership=nature,
            is_10b5_1=(xml_10b5_1 or fn_10b5_1),
            plan_10b5_1_source=plan_src,
            footnote_ids=fn_ids,
            footnotes=linked_fns,
        )

    t1: list[ParsedForm4Transaction] = []
    t2: list[ParsedForm4Transaction] = []
    nd_table = _child_by_local(root, "nonDerivativeTable")
    if nd_table is not None:
        for tx_node in _children_by_local(nd_table, "nonDerivativeTransaction"):
            parsed = parse_tx_node(tx_node, "I")
            if parsed is not None:
                t1.append(parsed)

    d_table = _child_by_local(root, "derivativeTable")
    if d_table is not None:
        for tx_node in _children_by_local(d_table, "derivativeTransaction"):
            parsed = parse_tx_node(tx_node, "II")
            if parsed is not None:
                t2.append(parsed)

    return RawForm4Parsed(
        cik=cik10(cik),
        accession=accession,
        form=doc_type if "/" in doc_type else form.upper(),
        filed_at=filed_at.isoformat(),
        period_of_report=period_str[:10] if period_str else None,
        date_of_original_submission=orig_sub_str[:10] if orig_sub_str else None,
        is_amendment=is_amendment,
        explicit_amends_accession=explicit_amends_acc,
        sec_url=sec_url,
        filename=filename,
        content_sha256=content_sha256,
        document_aff_10b5_1=doc_aff_10b5_1,
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        issuer_symbol=issuer_symbol,
        reporting_owners=owners,
        footnotes=footnotes_map,
        table_1_transactions=t1,
        table_2_transactions=t2,
        raw_xml_excerpt=_render_form4_raw_summary(owners, t1, t2, footnotes_map),
    )


def _render_form4_raw_summary(
    owners: Sequence[ReportingOwnerInfo],
    t1: Sequence[ParsedForm4Transaction],
    t2: Sequence[ParsedForm4Transaction],
    footnotes: Mapping[str, str],
) -> str:
    lines: list[str] = []
    for o in owners:
        roles = [
            r
            for flag, r in (
                (o.is_officer, f"Officer({o.officer_title or o.officer_tier})"),
                (o.is_director, "Director"),
                (o.is_ten_percent_owner, "10%Owner"),
                (o.is_other, o.other_text or "Other"),
            )
            if flag
        ]
        lines.append(f"ReportingOwner: {o.owner_name} (CIK {o.owner_cik}) [{', '.join(roles) or o.officer_tier}] tier={o.officer_tier}")
    for tx in t1:
        px = f"${tx.price_per_share}" if tx.price_per_share else "[PRICE_MISSING_OR_FOOTNOTE]"
        lines.append(
            f"Table I: {tx.transaction_date or '?'} code={tx.transaction_code} "
            f"({tx.acquired_disposed_code or '?'}) shares={tx.shares} price={px} "
            f"post={tx.post_shares or '?'} ownership={tx.direct_or_indirect} 10b5-1={tx.is_10b5_1}({tx.plan_10b5_1_source})"
        )
    for tx in t2:
        lines.append(
            f"Table II: {tx.transaction_date or '?'} {tx.security_title} code={tx.transaction_code} "
            f"shares={tx.shares} 10b5-1={tx.is_10b5_1}"
        )
    if footnotes:
        lines.append("Footnotes:")
        for fid, txt in footnotes.items():
            lines.append(f"  [{fid}] {txt}")
    return "\n".join(lines)


def parse_form4_raw(
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
) -> RawForm4Parsed:
    """Fetch raw Form 4 / 4/A XML (stripping XSLT prefix) and parse unfiltered."""
    xml_url, clean_fname = _resolve_form4_xml_url(
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
    xml_text, _ct, digest = sec_get_text(
        xml_url,
        category="ownership",
        user_agent=user_agent,
        limiter=limiter,
        policy=policy,
        transport=transport,
        stream_transport=stream_transport,
        accept="application/xml, text/xml, text/plain",
        max_bytes=5_000_000,
        cancel_event=cancel_event,
        deadline=deadline,
        telemetry=telemetry,
    )
    return parse_form4_xml_text(
        xml_text,
        cik=row.cik,
        accession=row.accession,
        form=row.form,
        filed_at=row.filed_at,
        sec_url=xml_url,
        filename=clean_fname,
        content_sha256=digest,
        rules=rules,
    )


def _primary_owner(owners: Sequence[ReportingOwnerInfo]) -> ReportingOwnerInfo:
    if not owners:
        return ReportingOwnerInfo(owner_cik="unknown", owner_name="Unknown Owner", officer_tier="director")
    return min(owners, key=lambda o: _TIER_RANK.get(o.officer_tier, 99))


def _aggregate_form4_groups(
    raw: RawForm4Parsed,
    rules: SecEventsRules,
) -> tuple[list[AggregatedTradeGroup], list[str]]:
    """Aggregate Table I transactions per ``(owner_cik, tx_date, code, is_10b5_1, D/I)`` and apply cashless split."""
    owner = _primary_owner(raw.reporting_owners)
    t1 = raw.table_1_transactions
    t2 = raw.table_2_transactions

    # Compute M (option exercise) shares per (tx_date, D/I) avoiding double counting Table I + Table II
    m_t1_by_day_di: dict[tuple[str, str], Decimal] = {}
    for tx in t1:
        if tx.transaction_code == "M":
            d = tx.transaction_date or raw.period_of_report or raw.filed_at
            key = (d, tx.direct_or_indirect)
            m_t1_by_day_di[key] = m_t1_by_day_di.get(key, Decimal(0)) + Decimal(tx.shares)

    m_t2_by_day: dict[str, Decimal] = {}
    for tx in t2:
        if tx.transaction_code in ("M", "C"):
            d = tx.transaction_date or raw.period_of_report or raw.filed_at
            m_t2_by_day[d] = m_t2_by_day.get(d, Decimal(0)) + Decimal(tx.shares)

    # Group Table I P and S transactions by (owner_cik, tx_date, code, is_10b5_1, D/I)
    keep_codes = set(rules.form4.keep_transaction_codes)
    buckets: dict[tuple[str, str, str, bool, str], list[ParsedForm4Transaction]] = {}
    excluded_codes_seen: list[str] = []
    for tx in t1:
        if tx.transaction_code not in keep_codes:
            if tx.transaction_code not in excluded_codes_seen:
                excluded_codes_seen.append(tx.transaction_code)
            continue
        tx_d = tx.transaction_date or raw.period_of_report or raw.filed_at
        key = (owner.owner_cik, tx_d, tx.transaction_code, tx.is_10b5_1, tx.direct_or_indirect)
        buckets.setdefault(key, []).append(tx)

    # Track remaining exercise shares per (tx_date, D/I) so non-10b5-1 sales claim M shares first
    remaining_m: dict[tuple[str, str], Decimal] = {}
    for (tx_d, di), amt in m_t1_by_day_di.items():
        remaining_m[(tx_d, di)] = amt
    for tx_d, amt in m_t2_by_day.items():
        if (tx_d, "D") not in remaining_m:
            remaining_m[(tx_d, "D")] = amt

    ordered_keys = sorted(buckets.keys(), key=lambda k: (k[1], k[2], k[3], k[4]))
    groups: list[AggregatedTradeGroup] = []
    for key in ordered_keys:
        o_cik, tx_d, code, is_10b5_1, di = key
        txs = buckets[key]
        tot_shares = sum((Decimal(tx.shares) for tx in txs), Decimal(0))
        missing_price = any(tx.price_status == "missing_or_footnote_only" or tx.price_per_share is None for tx in txs)

        weighted_avg: Decimal | None = None
        tot_val: Decimal | None = None
        if not missing_price:
            tot_val = sum((Decimal(tx.shares) * Decimal(tx.price_per_share or "0") for tx in txs), Decimal(0))
            if tot_shares > 0:
                weighted_avg = (tot_val / tot_shares).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

        cashless_shares = Decimal(0)
        disc_net_shares = tot_shares if code == "S" else Decimal(0)
        retained_shares = Decimal(0)
        is_cashless = False
        split_kind: Literal["none", "full", "partial_exercise_to_cover", "partial_excess_discretionary_sale"] = "none"

        if code == "S":
            avail_m = remaining_m.get((tx_d, di), Decimal(0))
            if avail_m > 0:
                is_cashless = True
                cashless_shares = min(avail_m, tot_shares)
                disc_net_shares = max(Decimal(0), tot_shares - avail_m)
                retained_shares = max(Decimal(0), avail_m - tot_shares)
                remaining_m[(tx_d, di)] = max(Decimal(0), avail_m - cashless_shares)
                if tot_shares == avail_m:
                    split_kind = "full"
                elif avail_m > tot_shares:
                    split_kind = "partial_exercise_to_cover"
                else:
                    split_kind = "partial_excess_discretionary_sale"

        disc_val: Decimal | None = None
        if code == "S" and tot_val is not None and tot_shares > 0:
            if disc_net_shares == tot_shares:
                disc_val = tot_val
            elif disc_net_shares == 0:
                disc_val = Decimal(0)
            else:
                disc_val = ((tot_val * disc_net_shares) / tot_shares).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # Same-XML post_shares / pre_shares handling (safe on 100% exit)
        post_candidates = [Decimal(tx.post_shares) for tx in txs if tx.post_shares is not None]
        ending_post: Decimal | None = None
        if post_candidates:
            ending_post = min(post_candidates) if code == "S" else max(post_candidates)

        pre_shares: Decimal | None = None
        pct_change: Decimal | None = None
        is_100pct_exit = False
        is_new_pos = False
        if ending_post is not None:
            if code == "S":
                pre_shares = ending_post + tot_shares
                is_100pct_exit = ending_post == 0 and tot_shares > 0
                if pre_shares > 0:
                    pct_change = ((tot_shares / pre_shares) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                pre_shares = max(Decimal(0), ending_post - tot_shares)
                if pre_shares > 0:
                    pct_change = ((tot_shares / pre_shares) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                elif tot_shares > 0:
                    is_new_pos = True

        # Combine plan_10b5_1_source
        sources = {tx.plan_10b5_1_source for tx in txs if tx.plan_10b5_1_source != "none"}
        if "both" in sources or ("xml_flag" in sources and "footnote" in sources):
            src_label: Literal["xml_flag", "footnote", "both", "none"] = "both"
        elif "xml_flag" in sources:
            src_label = "xml_flag"
        elif "footnote" in sources:
            src_label = "footnote"
        else:
            src_label = "none"

        fn_texts = list(dict.fromkeys(f["text"] for tx in txs for f in tx.footnotes if f.get("text")))
        nature = next((tx.nature_of_ownership for tx in txs if tx.nature_of_ownership), None)

        groups.append(
            AggregatedTradeGroup(
                owner_cik=o_cik,
                owner_name=owner.owner_name,
                officer_tier=owner.officer_tier,
                transaction_date=tx_d,
                transaction_code=code,  # type: ignore[arg-type]
                is_10b5_1=is_10b5_1,
                plan_10b5_1_source=src_label,
                direct_or_indirect=di,  # type: ignore[arg-type]
                nature_of_ownership=nature,
                total_shares=format(tot_shares, "f"),
                cashless_exercise_shares=format(cashless_shares, "f"),
                discretionary_net_sale_shares=format(disc_net_shares, "f"),
                shares_retained_from_exercise=format(retained_shares, "f"),
                is_cashless_exercise=is_cashless,
                cashless_split_kind=split_kind,
                price_status="missing_or_footnote_only" if missing_price else "explicit",
                weighted_avg_price_usd=format(weighted_avg, "f") if weighted_avg is not None else None,
                total_value_usd=format(tot_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f") if tot_val is not None else None,
                discretionary_value_usd=format(disc_val, "f") if disc_val is not None else None,
                post_shares=format(ending_post, "f") if ending_post is not None else None,
                pre_shares=format(pre_shares, "f") if pre_shares is not None else None,
                pct_holding_change=format(pct_change, "f") if pct_change is not None else None,
                is_100pct_exit=is_100pct_exit,
                is_new_position=is_new_pos,
                footnote_texts=fn_texts,
            )
        )

    return groups, excluded_codes_seen


def _filter_trade_groups(
    groups: Sequence[AggregatedTradeGroup],
    rules: SecEventsRules,
) -> tuple[list[AggregatedTradeGroup], list[str]]:
    """Filter aggregated trade groups using externalized dust floor, 10b5-1, and cashless rules."""
    kept: list[AggregatedTradeGroup] = []
    reasons: list[str] = []
    dust_floor = rules.form4.dust_floor_usd

    for grp in groups:
        if rules.form4.exclude_confirmed_10b5_1 and grp.is_10b5_1:
            reasons.append("routine_10b5_1_plan")
            continue
        if grp.transaction_code == "S" and grp.is_cashless_exercise and Decimal(grp.discretionary_net_sale_shares) <= 0:
            reasons.append("cashless_option_exercise")
            continue
        if grp.price_status == "missing_or_footnote_only":
            if rules.form4.exempt_missing_price_from_dust_floor:
                kept.append(grp)
            else:
                reasons.append("missing_price")
            continue
        effective_val = (
            Decimal(grp.discretionary_value_usd)
            if (grp.transaction_code == "S" and grp.is_cashless_exercise and grp.discretionary_value_usd is not None)
            else Decimal(grp.total_value_usd or "0")
        )
        if effective_val < dust_floor:
            reasons.append("below_dust_floor")
            continue
        kept.append(grp)

    return kept, reasons


def process_form4_batch(
    raw_filings: Sequence[RawForm4Parsed],
    rules: SecEventsRules,
    *,
    target_start: date,
    context_start: date,
    as_of: date,
    symbol: str | None = None,
    company_name: str | None = None,
) -> tuple[list[RawEventCandidate], list[SuppressedEventRecord], dict[str, str], dict[str, Any]]:
    """Apply Form 4 rules across all parsed filings: 4/A dedup, 14-day clusters, and 12m baseline."""
    # Sort chronologically oldest-first for clean amendment & baseline calculation
    ordered = sorted(
        [r for r in raw_filings if date.fromisoformat(r.filed_at) <= as_of],
        key=lambda r: (r.filed_at, r.accession),
    )

    # 1. Pre-compute unfiltered groups & detect 4/A supersession chains
    groups_by_acc: dict[str, list[AggregatedTradeGroup]] = {}
    excluded_by_acc: dict[str, list[str]] = {}
    superseded_by: dict[str, str] = {}
    amends_map: dict[str, str] = {}
    thread_map: dict[str, str] = {}

    for raw in ordered:
        groups, excl = _aggregate_form4_groups(raw, rules)
        groups_by_acc[raw.accession] = groups
        excluded_by_acc[raw.accession] = excl

    by_owner_period: dict[tuple[str, str], list[RawForm4Parsed]] = {}
    for raw in ordered:
        owner = _primary_owner(raw.reporting_owners)
        anchor = raw.date_of_original_submission or raw.period_of_report or raw.filed_at
        key = (owner.owner_cik, anchor)
        if raw.is_amendment:
            prior_acc = raw.explicit_amends_accession
            if prior_acc is None:
                candidates = by_owner_period.get(key, [])
                if candidates:
                    prior_acc = candidates[-1].accession
            if prior_acc:
                amends_map[raw.accession] = prior_acc
                superseded_by[prior_acc] = raw.accession
                thread_map[raw.accession] = thread_map.get(prior_acc, f"f4:{raw.cik}:{owner.owner_cik}:{anchor}")
            else:
                thread_map[raw.accession] = f"f4:{raw.cik}:{owner.owner_cik}:{anchor}"
        else:
            thread_map[raw.accession] = f"f4:{raw.cik}:{owner.owner_cik}:{anchor}"
        by_owner_period.setdefault(key, []).append(raw)

    # 2. Build kept vs suppressed per filing in target/context windows, plus 12m behavioral history
    candidates_raw: list[tuple[RawForm4Parsed, list[AggregatedTradeGroup], bool]] = []
    suppressed: list[SuppressedEventRecord] = []
    source_texts: dict[str, str] = {}

    # Track prior trades (non-superseded) strictly before/up-to each filing for 12m baseline
    baseline_records: list[tuple[date, str, str, bool, Decimal | None]] = []

    for raw in ordered:
        filed_d = date.fromisoformat(raw.filed_at)
        groups = groups_by_acc[raw.accession]
        is_superseded = raw.accession in superseded_by

        if not is_superseded:
            for grp in groups:
                val = Decimal(grp.total_value_usd) if grp.total_value_usd is not None else None
                baseline_records.append((filed_d, grp.owner_cik, grp.transaction_code, grp.is_10b5_1, val))

        if filed_d < context_start:
            continue

        is_ctx_only = filed_d < target_start
        kept_groups, filter_reasons = _filter_trade_groups(groups, rules)
        if not kept_groups:
            if not is_ctx_only:
                reason = filter_reasons[0] if filter_reasons else (
                    "non_open_market_code_only" if excluded_by_acc[raw.accession] else "no_qualifying_transactions"
                )
                suppressed.append(
                    SuppressedEventRecord(
                        accession=raw.accession,
                        form=raw.form,
                        filed_at=filed_d,
                        category="insider_trade",
                        suppression_reason=reason,
                        items=[t.transaction_code for t in raw.table_1_transactions],
                        summary_hint=f"Form {raw.form} by {_primary_owner(raw.reporting_owners).owner_name}: {reason}",
                    )
                )
            continue

        candidates_raw.append((raw, kept_groups, is_ctx_only))

    # 3. Compute 14-day clusters (non-superseded kept trades within window) & 12m baseline
    cluster_days = rules.form4.cluster_window_days
    baseline_days = rules.limits.baseline_lookback_days

    out_candidates: list[RawEventCandidate] = []
    for raw, kept_groups, is_ctx_only in candidates_raw:
        filed_d = date.fromisoformat(raw.filed_at)
        owner = _primary_owner(raw.reporting_owners)
        is_superseded = raw.accession in superseded_by
        tx_dates = [date.fromisoformat(g.transaction_date) for g in kept_groups if g.transaction_date]
        earliest_tx = min(tx_dates) if tx_dates else (date.fromisoformat(raw.period_of_report) if raw.period_of_report else filed_d)
        codes_present = sorted({g.transaction_code for g in kept_groups})
        primary_code = codes_present[0] if len(codes_present) == 1 else ("P" if "P" in codes_present else "S")

        # 14-day cluster (only non-superseded filings with filed_at <= filed_d so no future leak!)
        cluster_insiders: dict[str, dict[str, str]] = {}
        cluster_shares = Decimal(0)
        cluster_val = Decimal(0)
        if not is_superseded:
            for o_raw, o_groups, _ in candidates_raw:
                if o_raw.accession in superseded_by:
                    continue
                o_filed = date.fromisoformat(o_raw.filed_at)
                if o_filed > filed_d:
                    continue
                for og in o_groups:
                    if og.transaction_code != primary_code:
                        continue
                    og_d = date.fromisoformat(og.transaction_date)
                    if abs((earliest_tx - og_d).days) <= cluster_days:
                        cluster_insiders[og.owner_cik] = {
                            "owner_cik": og.owner_cik,
                            "owner_name": og.owner_name,
                            "officer_tier": og.officer_tier,
                        }
                        cluster_shares += Decimal(og.total_shares)
                        if og.total_value_usd is not None:
                            cluster_val += Decimal(og.total_value_usd)

        # 12-month behavioral baseline (strictly prior to or on filed_d excluding self? Use strictly prior filings < filed_d or other owners!)
        win_start = filed_d - timedelta(days=baseline_days)
        prior_owner_buys = 0
        prior_owner_sells_disc = 0
        prior_owner_sells_10b5 = 0
        prior_vals: list[Decimal] = []
        prior_company_buys = 0
        prior_company_sells_disc = 0
        for b_filed, b_owner, b_code, b_10b5, b_val in baseline_records:
            if not (win_start <= b_filed < filed_d):
                continue
            if b_code == "P" and not b_10b5:
                prior_company_buys += 1
                if b_owner == owner.owner_cik:
                    prior_owner_buys += 1
                    if b_val is not None:
                        prior_vals.append(b_val)
            elif b_code == "S":
                if b_10b5:
                    if b_owner == owner.owner_cik:
                        prior_owner_sells_10b5 += 1
                else:
                    prior_company_sells_disc += 1
                    if b_owner == owner.owner_cik:
                        prior_owner_sells_disc += 1
                        if b_val is not None:
                            prior_vals.append(b_val)

        cur_val = sum((Decimal(g.total_value_usd) for g in kept_groups if g.total_value_usd is not None), Decimal(0))
        avg_val = (sum(prior_vals, Decimal(0)) / Decimal(len(prior_vals))) if prior_vals else None
        size_ratio = (
            format((cur_val / avg_val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
            if avg_val and avg_val > 0 and cur_val > 0
            else None
        )

        nodash = raw.accession.replace("-", "")
        source_id = f"sec_event:{raw.cik}:{nodash}:form4"
        excerpt = _build_form4_candidate_excerpt(raw, kept_groups, amends_map.get(raw.accession), superseded_by.get(raw.accession))
        source_texts[source_id] = f"{excerpt}\n\n{raw.raw_xml_excerpt}".strip()

        lag_days = (filed_d - earliest_tx).days
        facts: dict[str, Any] = {
            "primary_owner": owner.model_dump(mode="json"),
            "reporting_owners": [o.model_dump(mode="json") for o in raw.reporting_owners],
            "aggregated_trades": [g.model_dump(mode="json") for g in kept_groups],
            "table_2_count": len(raw.table_2_transactions),
            "is_amendment": raw.is_amendment,
            "superseded_by_accession": superseded_by.get(raw.accession),
            "has_missing_price_exemption": any(g.price_status == "missing_or_footnote_only" for g in kept_groups),
            "has_cashless_exercise_split": any(g.is_cashless_exercise for g in kept_groups),
            "has_100pct_exit": any(g.is_100pct_exit for g in kept_groups),
            "cluster_context": {
                "window_days": cluster_days,
                "transaction_code": primary_code,
                "distinct_insiders": len(cluster_insiders),
                "is_cluster": len(cluster_insiders) >= 2,
                "insiders": list(cluster_insiders.values()),
                "cluster_total_shares": format(cluster_shares, "f"),
                "cluster_total_value_usd": format(cluster_val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
            },
            "behavioral_baseline": {
                "lookback_days": baseline_days,
                "owner_prior_12m_buys": prior_owner_buys,
                "owner_prior_12m_discretionary_sells": prior_owner_sells_disc,
                "owner_prior_12m_10b5_1_sells": prior_owner_sells_10b5,
                "company_prior_12m_buys": prior_company_buys,
                "company_prior_12m_discretionary_sells": prior_company_sells_disc,
                "is_first_open_market_buy_in_12m": (primary_code == "P" and prior_owner_buys == 0),
                "is_first_discretionary_sale_in_12m": (primary_code == "S" and prior_owner_sells_disc == 0),
                "size_vs_12m_avg_ratio": size_ratio,
            },
        }

        out_candidates.append(
            RawEventCandidate(
                candidate_id=deterministic_signal_id("f4", raw.cik, raw.accession),
                cik=raw.cik,
                symbol=symbol or raw.issuer_symbol,
                company_name=company_name or raw.issuer_name,
                category="insider_trade",
                form=raw.form,
                accession=raw.accession,
                amends_accession=amends_map.get(raw.accession),
                event_thread_key=thread_map[raw.accession],
                filed_at=filed_d,
                event_date=earliest_tx,
                reporting_lag_days=lag_days,
                items=codes_present,
                statutory_critical_items=[],
                facts=facts,
                excerpt=excerpt,
                source_id=source_id,
                sec_url=raw.sec_url,
                filename=raw.filename,
                content_sha256=raw.content_sha256 or sha256(excerpt.encode("utf-8")).hexdigest(),
                lazy_attachments=[],
                is_context_only=is_ctx_only,
            )
        )

    company_baseline_summary = {
        "total_12m_discretionary_buys": sum(1 for _f, _o, c, p, _v in baseline_records if c == "P" and not p),
        "total_12m_discretionary_sells": sum(1 for _f, _o, c, p, _v in baseline_records if c == "S" and not p),
        "total_12m_10b5_1_sells": sum(1 for _f, _o, c, p, _v in baseline_records if c == "S" and p),
    }
    return out_candidates, suppressed, source_texts, company_baseline_summary


def _build_form4_candidate_excerpt(
    raw: RawForm4Parsed,
    kept_groups: Sequence[AggregatedTradeGroup],
    amends_accession: str | None,
    superseded_by_accession: str | None,
) -> str:
    owner = _primary_owner(raw.reporting_owners)
    lines = [
        f"Form {raw.form} ({raw.accession}) filed {raw.filed_at} for {raw.issuer_name or raw.cik} ({raw.issuer_symbol or ''}).",
        f"Primary Insider: {owner.owner_name} (tier={owner.officer_tier}, title={owner.officer_title or 'N/A'}, total_owners={len(raw.reporting_owners)}).",
    ]
    if amends_accession:
        lines.append(f"Amendment notice: amends prior Form 4 {amends_accession}.")
    if superseded_by_accession:
        lines.append(f"Superseded notice: superseded by Form 4/A {superseded_by_accession}.")
    for grp in kept_groups:
        val_txt = f"${grp.total_value_usd}" if grp.total_value_usd else f"[price_status={grp.price_status}]"
        pct_txt = f"{grp.pct_holding_change}%" if grp.pct_holding_change else ("new_position" if grp.is_new_position else "N/A")
        extra = ""
        if grp.is_cashless_exercise:
            extra = (
                f" [cashless_split={grp.cashless_split_kind}: cashless={grp.cashless_exercise_shares}, "
                f"discretionary_net_sale={grp.discretionary_net_sale_shares}]"
            )
        if grp.is_100pct_exit:
            extra += " [100%_EXIT_POST_SHARES_ZERO]"
        lines.append(
            f"- {grp.transaction_date} | {grp.transaction_code} | ownership={grp.direct_or_indirect}"
            f"{f' ({grp.nature_of_ownership})' if grp.nature_of_ownership else ''} | "
            f"shares={grp.total_shares} | value={val_txt} | post_shares={grp.post_shares or '?'} | "
            f"holding_change={pct_txt}{extra}"
        )
        for fn in grp.footnote_texts:
            lines.append(f"  Footnote: {fn}")
    return "\n".join(lines)


__all__ = [
    "AggregatedTradeGroup",
    "ParsedForm4Transaction",
    "RawForm4Parsed",
    "ReportingOwnerInfo",
    "normalize_officer_tier",
    "parse_form4_raw",
    "parse_form4_xml_text",
    "process_form4_batch",
    "strip_xsl_from_url",
    "strip_xsl_prefix",
]
