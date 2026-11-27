"""Multi-format exporter (Markdown, CSV, OpenXML XLSX) for locked EarningsDossier artifacts.

Strictly enforces source ``Permissions.export`` boundaries: any data backed by
``export=False`` sources (such as BYOK FMP consensus or call transcripts) is
redacted/excluded across every exported format, with an explicit export-redaction
notice always included. Uses ``openpyxl`` when available and otherwise builds a
valid OpenXML ``.xlsx`` workbook purely with Python stdlib ``zipfile``.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from decimal import Decimal
import io
import math
import re
from typing import Any, Iterable, Literal, Mapping, Sequence
from xml.sax.saxutils import escape as xml_escape
import zipfile

from pydantic import BaseModel, ConfigDict, Field

from .earnings_external_data import EarningsConsensus
from .research_earnings_diagnostics import (
    DiagnosticFlag,
    DiagnosticMetric,
    EarningsQualityDiagnosticsResult,
    GuidanceAndNonGaapResult,
    PeriodFinancials,
    ValuationAndExpectationsResult,
)
from .research_earnings_squad import (
    BearCaseItem,
    EarningsDossier,
    EvidenceCard,
    EvidenceCitation,
    RejectedBearCase,
    SquadGap,
)

ExportFormat = Literal["markdown", "csv", "xlsx"]
MarkdownReportMode = Literal["auto", "memo", "deep_report"]

XLSX_SHEET_NAMES: tuple[str, ...] = (
    "Summary & Valuation",
    "Quarterly Financials",
    "Earnings Quality",
    "Segments & KPIs",
    "Guidance & Non-GAAP",
    "Bear Case & Sources",
)

EXPORT_MEDIA_TYPES: dict[ExportFormat, str] = {
    "markdown": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

EXPORT_EXTENSIONS: dict[ExportFormat, str] = {
    "markdown": "md",
    "csv": "csv",
    "xlsx": "xlsx",
}

_XML_INVALID_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SHEET_INVALID_CHARS_RE = re.compile(r"[\[\]:*?/\\]")
_GENERIC_SPEAKERS = frozenset({"operator", "analyst", "management", "speaker", "participant"})


class ExportUnavailableError(ValueError):
    """Raised when the requested export mode or format cannot be rendered."""


class ExportRedactionRecord(BaseModel):
    """Audit entry describing one source/scope redacted due to ``export=False``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    scope: str
    reason: str


class _SanitizedEvidenceCard(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    card_id: str
    worker_role: str
    topic_tag: str
    finding: str
    supporting_fact_ids: list[str] = Field(default_factory=list)
    citations: list[EvidenceCitation] = Field(default_factory=list)
    caveat: str | None = None
    unresolved_gaps: list[str] = Field(default_factory=list)
    numeric_claims: list[dict[str, Any]] = Field(default_factory=list)
    redacted: bool = False
    redacted_sources: list[str] = Field(default_factory=list)


class _SanitizedBearCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bear_id: str
    severity: str
    anchor_kind: str
    anchored_evidence_ids: list[str] = Field(default_factory=list)
    thesis_challenge: str
    next_quarter_invalidation_trigger: str
    supporting_numbers: list[str] = Field(default_factory=list)
    redacted: bool = False
    redacted_sources: list[str] = Field(default_factory=list)


class SanitizedDossierView(BaseModel):
    """Permission-sanitized view of a locked ``EarningsDossier`` safe for export."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dossier: EarningsDossier
    non_exportable_sources: list[str] = Field(default_factory=list)
    consensus_redacted: bool = False
    transcript_redacted: bool = False
    redacted_card_ids: list[str] = Field(default_factory=list)
    redactions: list[ExportRedactionRecord] = Field(default_factory=list)
    cards: list[_SanitizedEvidenceCard] = Field(default_factory=list)
    bear_cases: list[_SanitizedBearCase] = Field(default_factory=list)
    sanitized_memo_markdown: str = ""
    sanitized_deep_report_markdown: str | None = None
    redaction_notice_lines: list[str] = Field(default_factory=list)


class DossierExportResult(BaseModel):
    """Exported dossier payload, media headers, and permission audit summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dossier_id: str
    dossier_sha256: str
    format: ExportFormat
    report_mode: Literal["memo", "deep_report"] | None = None
    filename: str
    media_type: str
    content_bytes: bytes
    exportable_unrestricted: bool
    redacted_sources: tuple[str, ...] = ()
    redactions: tuple[ExportRedactionRecord, ...] = ()

    @property
    def text(self) -> str:
        if self.format == "xlsx":
            raise TypeError("XLSX export is binary; inspect content_bytes.")
        return self.content_bytes.decode("utf-8")


# --------------------------------------------------------------------------- Formatting Helpers
def _fmt_decimal(value: Decimal | int | float | str | None, *, places: int | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return ""
        if places is not None:
            return f"{value:.{places}f}"
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        return str(value)
    return str(value)


def _num_or_str(value: Decimal | int | float | None) -> int | float | Decimal | str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        if not value.is_finite():
            return ""
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return value


def _safe_filename(dossier: EarningsDossier, fmt: ExportFormat) -> str:
    stem_raw = dossier.symbol or dossier.company_name or dossier.entity_id or dossier.dossier_id
    stem = _UNSAFE_FILENAME_RE.sub("-", str(stem_raw).strip()).strip(".-") or "earnings-dossier"
    period_raw = _UNSAFE_FILENAME_RE.sub("-", str(dossier.period_label or "").strip()).strip(".-")
    suffix = f"-{period_raw}" if period_raw else ""
    ext = EXPORT_EXTENSIONS[fmt]
    return f"{stem}{suffix}-earnings-dossier.{ext}"


def _collect_exportable_numeric_tokens(dossier: EarningsDossier) -> set[str]:
    """Collect numeric strings already backed by exportable SEC statements/diagnostics so we never over-redact."""
    tokens: set[str] = set()
    for stmt in dossier.snapshot.statements:
        for fname in (
            "revenue", "cost_of_revenue", "gross_profit", "rd_expense", "sga_expense",
            "operating_income", "ebit", "interest_expense", "net_income", "ebitda",
            "depreciation_amortization", "sbc", "shares_diluted", "operating_cash_flow",
            "capex", "free_cash_flow", "share_repurchases", "dividends_paid", "cash",
            "short_term_investments", "accounts_receivable", "inventory", "current_assets",
            "accounts_payable", "short_term_debt", "long_term_debt", "total_debt",
            "current_liabilities", "minority_interest",
        ):
            mv = getattr(stmt, fname, None)
            if mv is not None and getattr(mv, "value", None) is not None:
                tokens.add(_fmt_decimal(mv.value))
                tokens.add(str(mv.value))
    # Register V4a bank disclosure, diagnostics, Gordon valuation & segment tokens as exportable
    b_disc = getattr(dossier.snapshot, "bank_disclosure", None)
    if b_disc is not None:
        for attr in (
            "net_revenue_gaap_q_usd", "net_revenue_managed_q_usd", "nii_gaap_q_usd", "ppnr_gaap_q_usd",
            "provision_q_usd", "net_income_to_common_q_usd", "cet1_standardized_pct", "cet1_reg_min_pct",
            "htm_net_unrealized_pnl_usd", "rotce_q_reported_pct", "efficiency_ratio_reported_pct",
        ):
            v = getattr(b_disc, attr, None)
            if v is not None:
                tokens.add(str(v))
        br = getattr(b_disc, "tbvps_bridge", None)
        if br is not None:
            for attr in ("tbvps_reported_usd", "tbvps_xbrl_std_usd", "tbvps_gap_pct", "tce_reported_usd", "tce_xbrl_std_usd"):
                v = getattr(br, attr, None)
                if v is not None:
                    tokens.add(str(v))
        segs = getattr(b_disc, "segments", None)
        if segs is not None:
            for sr in (*(segs.operating_segments or ()), *(segs.corporate_or_other or ()), *(segs.reconciling_items or ())):
                for attr in ("net_revenue_usd", "ppnr_usd", "net_income_usd", "revenue_share_pct", "segment_roe_or_rotce_pct"):
                    v = getattr(sr, attr, None)
                    if v is not None:
                        tokens.add(str(v))
    b_val = getattr(dossier.snapshot, "bank_valuation", None)
    if b_val is not None:
        for attr in ("price_usd", "p_to_tbv_primary", "p_to_tbv_xbrl_std", "p_to_bv", "p_to_e_ttm", "implied_rotce_pct", "rotce_spread_vs_implied_pp", "justified_p_to_tbv_at_current_rotce"):
            v = getattr(b_val, attr, None)
            if v is not None:
                tokens.add(str(v))
    b_diag = getattr(dossier.snapshot, "bank_diagnostics", None)
    if b_diag is not None:
        for attr in ("efficiency_ratio_gaap_pct", "ppnr_margin_pct", "nii_share_pct", "rotce_q_pct", "cet1_buffer_bps", "htm_loss_pct_of_tce", "pro_forma_cet1_ex_htm_pct", "provision_to_ppnr_pct", "ldr_pct"):
            v = getattr(b_diag, attr, None)
            if v is not None:
                tokens.add(str(v))
    return {t for t in tokens if t}


def _collect_consensus_numbers(records: Sequence[EarningsConsensus], *, safe_tokens: set[str]) -> set[str]:
    out: set[str] = set()
    for rec in records:
        for fname in (
            "revenue_estimate_avg", "revenue_estimate_low", "revenue_estimate_high",
            "eps_estimate_avg", "eps_estimate_low", "eps_estimate_high",
            "revenue_actual", "eps_actual",
        ):
            val = getattr(rec, fname, None)
            if val is None:
                continue
            for candidate in (_fmt_decimal(val), str(val)):
                if len(candidate) >= 3 and candidate not in safe_tokens:
                    out.add(candidate)
    return out


# --------------------------------------------------------------------------- Sanitization & Export-Permission Enforcement
def sanitize_dossier_for_export(
    dossier: EarningsDossier,
    *,
    extra_source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> SanitizedDossierView:
    """Enforce ``export=False`` source permissions across all sections of a locked ``EarningsDossier``."""
    snap = dossier.snapshot
    non_exportable: set[str] = set(dossier.gate_report.non_exportable_sources)
    redactions: list[ExportRedactionRecord] = []
    seen_redactions: set[tuple[str, str]] = set()

    def add_redaction(source_id: str, scope: str, reason: str) -> None:
        key = (source_id, scope)
        if key not in seen_redactions:
            seen_redactions.add(key)
            redactions.append(ExportRedactionRecord(source_id=source_id, scope=scope, reason=reason))

    for ref in snap.prefetched_sources:
        if ref.permissions.get("export") is False or ref.source_id in non_exportable:
            non_exportable.add(ref.source_id)
            if len(ref.title.strip()) >= 6:
                # Only treat ref.title as restricted once we append to restricted list below.
                pass
            add_redaction(ref.source_id, "prefetched_source", f"Source '{ref.source_id}' has export=False.")

    if extra_source_permissions:
        for sid, perms in extra_source_permissions.items():
            if isinstance(perms, Mapping) and perms.get("export") is False:
                non_exportable.add(sid)
                add_redaction(sid, "topic_source", f"Source '{sid}' has export=False.")

    consensus_src_id = snap.consensus_summary.source_id or "fmp_consensus"
    consensus_redacted = (
        snap.consensus_summary.permissions.get("export") is False
        or consensus_src_id in non_exportable
        or any(rec.permissions.export is False for rec in snap.consensus_summary.records)
        or any(
            rec.source_provider in non_exportable or f"{rec.source_provider}_consensus" in non_exportable
            for rec in snap.consensus_summary.records
        )
    )
    if consensus_redacted:
        non_exportable.add(consensus_src_id)
        for rec in snap.consensus_summary.records:
            if rec.permissions.export is False:
                non_exportable.add(f"{rec.source_provider}_consensus")
        add_redaction(
            consensus_src_id,
            "consensus_summary",
            "Street consensus estimates and consensus-derived NTM multiples excluded due to export=False.",
        )

    transcript_src_id = snap.transcript_index.source_id or "fmp_transcript"
    transcript_redacted = (
        snap.transcript_index.permissions.get("export") is False
        or transcript_src_id in non_exportable
        or (
            snap.transcript_index.provider is not None
            and (
                snap.transcript_index.provider in non_exportable
                or f"{snap.transcript_index.provider}_transcript" in non_exportable
            )
        )
    )
    if transcript_redacted:
        non_exportable.add(transcript_src_id)
        add_redaction(
            transcript_src_id,
            "transcript_index",
            "Earnings call transcript metadata/speakers/citations excluded due to export=False.",
        )

    safe_sec_tokens = _collect_exportable_numeric_tokens(dossier)
    exportable_corpus = "\n".join(
        part
        for card in dossier.evidence_cards
        for part in (
            [c.quotation for c in card.citations if c.source_id not in non_exportable]
        )
    )
    restricted_raw_texts: list[str] = []
    for ref in snap.prefetched_sources:
        if ref.source_id in non_exportable and len(ref.title.strip()) >= 6 and ref.title.strip().casefold() not in exportable_corpus.casefold():
            restricted_raw_texts.append(ref.title.strip())
    restricted_speakers: set[str] = set()
    restricted_consensus_nums: set[str] = set()

    if consensus_redacted:
        restricted_consensus_nums.update(
            _collect_consensus_numbers(snap.consensus_summary.records, safe_tokens=safe_sec_tokens)
        )
    if transcript_redacted:
        for spk in snap.transcript_index.speakers:
            cleaned = spk.strip()
            if len(cleaned) >= 4 and cleaned.casefold() not in _GENERIC_SPEAKERS:
                restricted_speakers.add(cleaned)

    redacted_card_ids: list[str] = []
    sanitized_cards: list[_SanitizedEvidenceCard] = []

    def _matches_restricted_source(token: str) -> str | None:
        for sid in non_exportable:
            if token == sid or token.startswith(f"{sid}:") or sid in token:
                return sid
        return None

    for card in dossier.evidence_cards:
        kept_cits: list[EvidenceCitation] = []
        card_restricted_srcs: set[str] = set()
        for cit in card.citations:
            if cit.source_id in non_exportable:
                card_restricted_srcs.add(cit.source_id)
                if len(cit.quotation.strip()) >= 6:
                    restricted_raw_texts.append(cit.quotation.strip())
                add_redaction(
                    cit.source_id,
                    f"evidence_citation:{card.card_id}",
                    f"Citation from '{cit.source_id}' on card '{card.card_id}' redacted (export=False).",
                )
            else:
                kept_cits.append(cit)

        fact_restricted_srcs: set[str] = set()
        for fid in card.supporting_fact_ids:
            matched = _matches_restricted_source(fid)
            if matched is not None:
                fact_restricted_srcs.add(matched)

        all_card_restricted = sorted(card_restricted_srcs | fact_restricted_srcs)
        fully_restricted = bool(all_card_restricted) and len(kept_cits) == 0

        if fully_restricted:
            redacted_card_ids.append(card.card_id)
            if len(card.finding.strip()) >= 6:
                restricted_raw_texts.append(card.finding.strip())
            if card.caveat and len(card.caveat.strip()) >= 6:
                restricted_raw_texts.append(card.caveat.strip())
            for claim in card.numeric_claims:
                for val in claim.values():
                    if isinstance(val, str) and len(val.strip()) >= 6:
                        restricted_raw_texts.append(val.strip())
            src_label = ", ".join(all_card_restricted)
            add_redaction(
                src_label,
                f"evidence_card:{card.card_id}",
                f"Evidence card '{card.card_id}' redacted because all backing sources ({src_label}) have export=False.",
            )
            sanitized_cards.append(_SanitizedEvidenceCard(
                card_id=card.card_id,
                worker_role=card.worker_role,
                topic_tag=card.topic_tag,
                finding=f"[REDACTED: backed by non-exportable source(s): {src_label}]",
                supporting_fact_ids=[fid for fid in card.supporting_fact_ids if _matches_restricted_source(fid) is None],
                citations=[],
                caveat="Excluded per source Permissions(export=False).",
                unresolved_gaps=list(card.unresolved_gaps),
                numeric_claims=[],
                redacted=True,
                redacted_sources=all_card_restricted,
            ))
        elif all_card_restricted:
            src_label = ", ".join(all_card_restricted)
            note = f"Note: citation(s) from non-exportable source(s) ({src_label}) excluded from export."
            caveat = f"{card.caveat} ({note})" if card.caveat else note
            sanitized_cards.append(_SanitizedEvidenceCard(
                card_id=card.card_id,
                worker_role=card.worker_role,
                topic_tag=card.topic_tag,
                finding=card.finding,
                supporting_fact_ids=list(card.supporting_fact_ids),
                citations=kept_cits,
                caveat=caveat,
                unresolved_gaps=list(card.unresolved_gaps),
                numeric_claims=list(card.numeric_claims),
                redacted=False,
                redacted_sources=all_card_restricted,
            ))
        else:
            sanitized_cards.append(_SanitizedEvidenceCard(
                card_id=card.card_id,
                worker_role=card.worker_role,
                topic_tag=card.topic_tag,
                finding=card.finding,
                supporting_fact_ids=list(card.supporting_fact_ids),
                citations=kept_cits,
                caveat=card.caveat,
                unresolved_gaps=list(card.unresolved_gaps),
                numeric_claims=list(card.numeric_claims),
            ))

    redacted_card_set = set(redacted_card_ids)
    sanitized_bears: list[_SanitizedBearCase] = []
    for bear in dossier.bear_cases:
        restricted_anchors: list[str] = []
        exportable_anchors: list[str] = []
        for anchor in bear.anchored_evidence_ids:
            if anchor in redacted_card_set or _matches_restricted_source(anchor) is not None:
                restricted_anchors.append(anchor)
            elif transcript_redacted and (anchor == "transcript:qa" or anchor.startswith("transcript:")):
                restricted_anchors.append(anchor)
            elif consensus_redacted and (
                anchor in {
                    "consensus",
                    "consensus:gap",
                    "pe_forward",
                    "valuation:pe_forward",
                    "ev_to_ntm_sales",
                    "valuation:ev_to_ntm_sales",
                    "ev_to_ntm_ebitda",
                    "valuation:ev_to_ntm_ebitda",
                }
                or anchor.startswith("consensus:")
            ):
                restricted_anchors.append(anchor)
            else:
                exportable_anchors.append(anchor)
        if restricted_anchors and not exportable_anchors:
            if len(bear.thesis_challenge.strip()) >= 6:
                restricted_raw_texts.append(bear.thesis_challenge.strip())
            if len(bear.next_quarter_invalidation_trigger.strip()) >= 6:
                restricted_raw_texts.append(bear.next_quarter_invalidation_trigger.strip())
            for num_item in bear.supporting_numbers:
                if isinstance(num_item, str) and len(num_item.strip()) >= 6:
                    restricted_raw_texts.append(num_item.strip())
            src_list = sorted(non_exportable) or ["non_exportable_source"]
            add_redaction(
                ", ".join(src_list),
                f"bear_case:{bear.bear_id}",
                f"Bear case '{bear.bear_id}' redacted because all anchors reference export=False sources.",
            )
            sanitized_bears.append(_SanitizedBearCase(
                bear_id=bear.bear_id,
                severity=bear.severity,
                anchor_kind=bear.anchor_kind,
                anchored_evidence_ids=[],
                thesis_challenge="[REDACTED: anchored solely to non-exportable source(s)]",
                next_quarter_invalidation_trigger="[REDACTED]",
                supporting_numbers=[],
                redacted=True,
                redacted_sources=src_list,
            ))
        else:
            sanitized_bears.append(_SanitizedBearCase(
                bear_id=bear.bear_id,
                severity=bear.severity,
                anchor_kind=bear.anchor_kind,
                anchored_evidence_ids=exportable_anchors or list(bear.anchored_evidence_ids),
                thesis_challenge=bear.thesis_challenge,
                next_quarter_invalidation_trigger=bear.next_quarter_invalidation_trigger,
                supporting_numbers=list(bear.supporting_numbers),
                redacted=False,
                redacted_sources=[a for a in restricted_anchors],
            ))

    restricted_phrases: set[str] = set()
    exportable_lower = exportable_corpus.casefold()
    for raw_item in restricted_raw_texts:
        cleaned = raw_item.strip()
        if len(cleaned) >= 6 and cleaned.casefold() not in exportable_lower:
            restricted_phrases.add(cleaned)
        for clause in re.split(r"[.;:\n]+", cleaned):
            c = clause.strip()
            if len(c) >= 12 and c.casefold() not in exportable_lower:
                restricted_phrases.add(c)
        words = cleaned.split()
        for idx in range(0, max(0, len(words) - 3)):
            window = " ".join(words[idx : idx + 4])
            if len(window) >= 18 and window.casefold() not in exportable_lower:
                restricted_phrases.add(window)
        for ident in re.findall(r"[A-Za-z0-9]*_[A-Za-z0-9_]{5,}|[A-Za-z0-9_]{16,}", cleaned):
            if ident.casefold() not in exportable_lower:
                restricted_phrases.add(ident)

    memo_md = _redact_markdown_text(
        dossier.memo_markdown,
        non_exportable_sources=non_exportable,
        restricted_phrases=restricted_phrases,
        redacted_card_ids=redacted_card_set,
        restricted_speakers=restricted_speakers,
        restricted_consensus_nums=restricted_consensus_nums,
        consensus_redacted=consensus_redacted,
        transcript_redacted=transcript_redacted,
    )
    deep_md = (
        _redact_markdown_text(
            dossier.deep_report_markdown,
            non_exportable_sources=non_exportable,
            restricted_phrases=restricted_phrases,
            redacted_card_ids=redacted_card_set,
            restricted_speakers=restricted_speakers,
            restricted_consensus_nums=restricted_consensus_nums,
            consensus_redacted=consensus_redacted,
            transcript_redacted=transcript_redacted,
        )
        if dossier.deep_report_markdown is not None
        else None
    )

    sorted_restricted = sorted(non_exportable)
    if sorted_restricted:
        notice_lines = [
            f"EXPORT REDACTION ACTIVE: Excluded non-exportable source(s): {', '.join(sorted_restricted)} (Permissions.export=False).",
            f"Redacted items count: {len(redactions)}. Official SEC disclosures and user-exportable artifacts remain intact.",
        ]
        for rec in redactions:
            notice_lines.append(f"[{rec.source_id}] ({rec.scope}): {rec.reason}")
    else:
        notice_lines = [
            "Export Permissions Verified: All referenced sources permit export (export=True); 0 redactions applied.",
        ]

    return SanitizedDossierView(
        dossier=dossier,
        non_exportable_sources=sorted_restricted,
        consensus_redacted=consensus_redacted,
        transcript_redacted=transcript_redacted,
        redacted_card_ids=redacted_card_ids,
        redactions=redactions,
        cards=sanitized_cards,
        bear_cases=sanitized_bears,
        sanitized_memo_markdown=memo_md,
        sanitized_deep_report_markdown=deep_md,
        redaction_notice_lines=notice_lines,
    )


def _redact_markdown_text(
    raw_text: str,
    *,
    non_exportable_sources: set[str],
    restricted_phrases: set[str],
    redacted_card_ids: set[str],
    restricted_speakers: set[str],
    restricted_consensus_nums: set[str],
    consensus_redacted: bool,
    transcript_redacted: bool,
) -> str:
    if not raw_text or (
        not non_exportable_sources
        and not restricted_phrases
        and not redacted_card_ids
        and not consensus_redacted
        and not transcript_redacted
    ):
        return raw_text

    text = raw_text
    # First replace exact restricted quotations/findings verbatim (longest first).
    for phrase in sorted(restricted_phrases, key=len, reverse=True):
        if phrase and phrase in text:
            text = text.replace(phrase, "[REDACTED: export=False]")

    out_lines: list[str] = []
    for line in text.splitlines():
        should_redact_line = False
        for sid in non_exportable_sources:
            if sid and re.search(rf"(?<![A-Za-z0-9_]){re.escape(sid)}(?![A-Za-z0-9_])", line):
                should_redact_line = True
                break
        if not should_redact_line:
            for cid in redacted_card_ids:
                if cid and re.search(rf"(?<![A-Za-z0-9_]){re.escape(cid)}(?![A-Za-z0-9_])", line):
                    should_redact_line = True
                    break
        if not should_redact_line and transcript_redacted:
            for spk in restricted_speakers:
                if re.search(rf"(?<![A-Za-z0-9_]){re.escape(spk)}(?![A-Za-z0-9_])", line):
                    should_redact_line = True
                    break
        if not should_redact_line and consensus_redacted:
            lowered = line.casefold()
            if any(num in line for num in restricted_consensus_nums) and any(
                kw in lowered for kw in ("consensus", "street", "estimate", "fmp", "ntm", "forward")
            ):
                should_redact_line = True

        if should_redact_line:
            prefix = "- " if line.lstrip().startswith(("- ", "* ")) else ""
            out_lines.append(f"{prefix}[REDACTED: content from export=False source excluded]")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _metric_value_is_redacted(
    metric: DiagnosticMetric,
    *,
    view: SanitizedDossierView,
    is_consensus_ntm: bool = False,
) -> bool:
    if any(sid in view.non_exportable_sources for sid in metric.source_ids):
        return True
    if is_consensus_ntm and view.consensus_redacted and metric.status == "computed":
        # Redact consensus-derived NTM forward valuation metrics unless explicitly backed only by an exportable source.
        if not metric.source_ids or any(sid in view.non_exportable_sources for sid in metric.source_ids):
            return True
    return False


def _format_diag_metric_value(
    metric: DiagnosticMetric,
    *,
    view: SanitizedDossierView,
    is_consensus_ntm: bool = False,
) -> int | float | Decimal | str:
    if _metric_value_is_redacted(metric, view=view, is_consensus_ntm=is_consensus_ntm):
        return "[REDACTED: export=False]"
    if metric.status != "computed" or metric.value is None:
        return ""
    return _num_or_str(metric.value)


def _metric_status_label(
    metric: DiagnosticMetric,
    *,
    view: SanitizedDossierView,
    is_consensus_ntm: bool = False,
) -> str:
    if _metric_value_is_redacted(metric, view=view, is_consensus_ntm=is_consensus_ntm):
        return "redacted_export_forbidden"
    return metric.status


# --------------------------------------------------------------------------- Tabular Data Builders (Shared by CSV & XLSX)
def _valuation_rows(view: SanitizedDossierView) -> list[list[Any]]:
    val: ValuationAndExpectationsResult | None = view.dossier.snapshot.valuation
    if val is None:
        return []
    rows: list[list[Any]] = []
    metric_specs: list[tuple[str, DiagnosticMetric, bool]] = [
        ("market_cap", val.market_cap, False),
        ("enterprise_value", val.enterprise_value, False),
        ("pe_ttm", val.pe_ttm, False),
        ("pe_forward", val.pe_forward, True),
        ("ev_to_sales", val.ev_to_sales, False),
        ("ev_to_ntm_sales", val.ev_to_ntm_sales, True),
        ("ev_to_ebitda", val.ev_to_ebitda, False),
        ("ev_to_ntm_ebitda", val.ev_to_ntm_ebitda, True),
        ("ev_to_ebit", val.ev_to_ebit, False),
        ("p_to_fcf", val.p_to_fcf, False),
        ("fcf_yield_pct", val.fcf_yield_pct, False),
        ("fcf_ex_sbc", val.fcf_ex_sbc, False),
        ("ev_to_fcf_ex_sbc", val.ev_to_fcf_ex_sbc, False),
        ("fcf_ex_sbc_yield_pct", val.fcf_ex_sbc_yield_pct, False),
    ]
    for mkey, mobj, is_ntm in metric_specs:
        redacted = _metric_value_is_redacted(mobj, view=view, is_consensus_ntm=is_ntm)
        rows.append([
            mkey,
            _format_diag_metric_value(mobj, view=view, is_consensus_ntm=is_ntm),
            mobj.unit,
            mobj.currency or val.currency or "",
            _metric_status_label(mobj, view=view, is_consensus_ntm=is_ntm),
            mobj.formula,
            "redacted" if redacted else ", ".join(mobj.input_ids or mobj.source_ids),
            "Redacted because consensus source has export=False" if redacted else (mobj.reason or ""),
        ])

    rdcf = val.reverse_dcf
    rows.append([
        "reverse_dcf_implied_5y_fcf_cagr_pct",
        _num_or_str(rdcf.implied_5y_fcf_cagr_pct) if rdcf.status == "computed" else "",
        "%",
        val.currency or "",
        rdcf.status,
        rdcf.formula,
        ", ".join(rdcf.input_ids),
        f"WACC={_fmt_decimal(rdcf.wacc_pct)}%, g={_fmt_decimal(rdcf.terminal_growth_pct)}%, TargetEV={_fmt_decimal(rdcf.target_ev)}"
        + (f" ({rdcf.reason})" if rdcf.reason else ""),
    ])
    return rows


_QUARTERLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("Revenue", "revenue"),
    ("Cost of Revenue", "cost_of_revenue"),
    ("Gross Profit", "gross_profit"),
    ("R&D Expense", "rd_expense"),
    ("SG&A Expense", "sga_expense"),
    ("Operating Income", "operating_income"),
    ("EBIT", "ebit"),
    ("Interest Expense", "interest_expense"),
    ("Net Income", "net_income"),
    ("EBITDA", "ebitda"),
    ("Depreciation & Amortization", "depreciation_amortization"),
    ("Stock-Based Compensation (SBC)", "sbc"),
    ("Operating Cash Flow", "operating_cash_flow"),
    ("CapEx", "capex"),
    ("Free Cash Flow", "free_cash_flow"),
    ("Share Repurchases", "share_repurchases"),
    ("Dividends Paid", "dividends_paid"),
    ("Cash & Equivalents", "cash"),
    ("Short-Term Investments", "short_term_investments"),
    ("Accounts Receivable", "accounts_receivable"),
    ("Inventory", "inventory"),
    ("Current Assets", "current_assets"),
    ("Accounts Payable", "accounts_payable"),
    ("Short-Term Debt", "short_term_debt"),
    ("Long-Term Debt", "long_term_debt"),
    ("Total Debt", "total_debt"),
    ("Current Liabilities", "current_liabilities"),
    ("Minority Interest", "minority_interest"),
    ("Diluted Shares", "shares_diluted"),
)


def _diagnostics_rows(view: SanitizedDossierView) -> tuple[list[list[Any]], list[list[Any]]]:
    diag: EarningsQualityDiagnosticsResult | None = view.dossier.snapshot.diagnostics
    metric_rows: list[list[Any]] = []
    flag_rows: list[list[Any]] = []

    if diag is not None:
        for mb in diag.margin_bridge:
            cur = mb.current_pct
            metric_rows.append([
                "margin_bridge",
                mb.line,
                _format_diag_metric_value(cur, view=view),
                cur.unit,
                cur.currency or diag.currency or "",
                cur.status,
                _fmt_decimal(mb.qoq_change_bps.value if mb.qoq_change_bps else None),
                _fmt_decimal(mb.yoy_change_bps.value if mb.yoy_change_bps else None),
                cur.formula,
                ", ".join(cur.input_ids),
                cur.reason or "",
            ])

        groups: list[tuple[str, Any, tuple[str, ...]]] = [
            ("earnings_quality", diag.earnings_quality, (
                "reported_or_derived_fcf", "ocf_to_ni", "fcf_to_ni", "fcf_margin_pct",
                "sbc_to_revenue_pct", "sbc_to_fcf_pct", "fcf_ex_sbc", "fcf_ex_sbc_margin_pct",
            )),
            ("capital_allocation", diag.capital_allocation, (
                "capex_to_revenue_pct", "capital_returned", "payout_to_fcf_pct", "diluted_shares_yoy_pct",
            )),
            ("balance_sheet", diag.balance_sheet, (
                "total_cash", "total_debt", "net_debt", "net_cash",
                "net_debt_to_ttm_ebitda", "interest_coverage", "current_ratio",
            )),
        ]
        for group_name, group_obj, fields in groups:
            for fname in fields:
                mobj: DiagnosticMetric = getattr(group_obj, fname)
                metric_rows.append([
                    group_name,
                    mobj.name,
                    _format_diag_metric_value(mobj, view=view),
                    mobj.unit,
                    mobj.currency or diag.currency or "",
                    mobj.status,
                    "",
                    "",
                    mobj.formula,
                    ", ".join(mobj.input_ids),
                    mobj.reason or "",
                ])

        for wc_name in ("ar_yoy_growth_pct", "revenue_yoy_growth_pct", "ar_vs_revenue_growth_pp",
                        "inventory_yoy_growth_pct", "cogs_yoy_growth_pct", "inventory_vs_cogs_growth_pp",
                        "inventory_vs_revenue_growth_pp"):
            mobj = getattr(diag.working_capital, wc_name)
            metric_rows.append([
                "working_capital",
                mobj.name,
                _format_diag_metric_value(mobj, view=view),
                mobj.unit,
                mobj.currency or "",
                mobj.status,
                "",
                "",
                mobj.formula,
                ", ".join(mobj.input_ids),
                mobj.reason or "",
            ])

        for wc_item_name in ("inventory_turnover", "dso", "dio", "dpo", "ccc"):
            item = getattr(diag.working_capital, wc_item_name)
            cur = item.current
            metric_rows.append([
                "working_capital",
                cur.name,
                _format_diag_metric_value(cur, view=view),
                cur.unit,
                cur.currency or "",
                cur.status,
                _fmt_decimal(item.qoq_delta.value if item.qoq_delta else None),
                _fmt_decimal(item.yoy_delta.value if item.yoy_delta else None),
                cur.formula,
                ", ".join(cur.input_ids),
                cur.reason or "",
            ])

    flags: list[DiagnosticFlag] = list(diag.flags if diag is not None else view.dossier.snapshot.flags)
    for fl in flags:
        flag_rows.append([
            fl.flag_id,
            fl.severity,
            fl.group,
            fl.metric_name,
            _num_or_str(fl.observed_value),
            fl.unit,
            fl.threshold_rule,
            fl.summary,
            ", ".join(fl.input_ids),
        ])
    return metric_rows, flag_rows


def _guidance_rows(view: SanitizedDossierView) -> tuple[list[list[Any]], list[list[Any]], list[list[Any]]]:
    gng: GuidanceAndNonGaapResult | None = view.dossier.snapshot.guidance_nongaap
    if gng is None:
        return [], [], []

    guid_rows: list[list[Any]] = []
    for ev in gng.guidance_evaluations:
        prior = ev.vs_prior_guidance
        cons = ev.vs_consensus
        ng = ev.new_guidance_direction
        cons_restricted = view.consensus_redacted or any(sid in view.non_exportable_sources for sid in cons.source_ids)
        guid_rows.append([
            ev.metric_name,
            ev.period_label or gng.period_label,
            _num_or_str(prior.actual_value if prior.actual_value is not None else cons.actual_value),
            prior.unit,
            prior.currency or "",
            _fmt_decimal(prior.guidance_low),
            _fmt_decimal(prior.benchmark_value),
            _fmt_decimal(prior.guidance_high),
            prior.outcome if prior.status == "computed" else prior.status,
            _fmt_decimal(prior.pct_diff),
            "[REDACTED: export=False]" if cons_restricted else _fmt_decimal(cons.benchmark_value),
            "redacted_export_forbidden" if cons_restricted else (cons.outcome if cons.status == "computed" else cons.status),
            "[REDACTED]" if cons_restricted else _fmt_decimal(cons.pct_diff),
            _fmt_decimal(ng.new_low),
            _fmt_decimal(ng.new_mid),
            _fmt_decimal(ng.new_high),
            ng.direction,
            _fmt_decimal(ng.pct_change),
            ", ".join(prior.input_ids),
        ])

    bridge_rows: list[list[Any]] = []
    for br in gng.nongaap_verifications:
        recurring = [adj.label for adj in br.classified_adjustments if adj.recurring_flag]
        bridge_rows.append([
            br.metric_name,
            br.period_label,
            br.unit,
            br.currency or "",
            _num_or_str(br.gaap_value),
            _num_or_str(br.reported_non_gaap_value),
            _num_or_str(br.adjustments_sum),
            _num_or_str(br.computed_non_gaap_value),
            _num_or_str(br.discrepancy),
            br.tie_status,
            _format_diag_metric_value(br.uplift_pct, view=view),
            ", ".join(recurring),
            br.formula,
            ", ".join(br.input_ids),
        ])

    warn_rows: list[list[Any]] = []
    for w in gng.warnings:
        warn_rows.append([
            w.flag_id,
            w.severity,
            w.metric_name,
            w.adjustment_label or "",
            w.category or "",
            w.summary,
            ", ".join(w.input_ids),
        ])
    return guid_rows, bridge_rows, warn_rows


# --------------------------------------------------------------------------- Format 1: Markdown Export
def export_dossier_markdown(
    dossier: EarningsDossier,
    *,
    report_mode: MarkdownReportMode = "auto",
    extra_source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> DossierExportResult:
    """Export a locked ``EarningsDossier`` as clean Markdown with citations & explicit gap/redaction notice."""
    view = sanitize_dossier_for_export(dossier, extra_source_permissions=extra_source_permissions)
    if report_mode == "deep_report":
        if view.sanitized_deep_report_markdown is None:
            raise ExportUnavailableError("Deep Report markdown has not been generated for this dossier yet.")
        active_mode: Literal["memo", "deep_report"] = "deep_report"
        narrative = view.sanitized_deep_report_markdown
    elif report_mode == "memo":
        active_mode = "memo"
        narrative = view.sanitized_memo_markdown
    else:
        if view.sanitized_deep_report_markdown is not None:
            active_mode = "deep_report"
            narrative = view.sanitized_deep_report_markdown
        else:
            active_mode = "memo"
            narrative = view.sanitized_memo_markdown

    snap = dossier.snapshot
    entity_display = dossier.symbol or dossier.company_name or dossier.entity_id
    lines: list[str] = [
        f"# Buy-Side Earnings Research Export: {entity_display} ({dossier.period_label or 'Latest Period'})",
        "",
        f"- **Dossier ID**: `{dossier.dossier_id}`",
        f"- **Run ID**: `{dossier.run_id}`",
        f"- **Digest (SHA-256)**: `{dossier.dossier_sha256}`",
        f"- **Report Mode**: `{active_mode}`",
        f"- **CIK**: `{dossier.cik or 'N/A'}`",
        f"- **Company**: {dossier.company_name or 'N/A'}",
        f"- **Selected L2 Pack**: `{dossier.selected_l2_pack_id or 'earnings_review (L1)'}`",
        "",
        "## Export Permissions & Redaction Notice",
    ]
    for msg in view.redaction_notice_lines:
        lines.append(f"- {msg}")

    lines.extend(["", "## Sector Applicability Gate"])
    sec_app = dossier.sector_applicability
    lines.append(f"- **Category**: `{sec_app.sector_category}` (SIC `{sec_app.sic_code or 'N/A'}`)")
    lines.append(f"- **Sector Gated**: `{sec_app.is_gated}`")
    lines.append(f"- **Explanation**: {sec_app.explanation}")
    if sec_app.disabled_metrics:
        lines.append(f"- **Disabled Metrics**: {', '.join(sec_app.disabled_metrics)}")
    if sec_app.recommended_alternatives:
        lines.append(f"- **Recommended Alternatives**: {', '.join(sec_app.recommended_alternatives)}")

    lines.extend(["", f"## Executive Narrative ({'Deep Report' if active_mode == 'deep_report' else 'Memo'})", ""])
    lines.append(narrative.strip() if narrative else "_No narrative recorded._")

    val_rows = _valuation_rows(view)
    if val_rows:
        lines.extend(["", "## Valuation & Implied Expectations", "", "| Metric | Value | Unit | Status | Notes |", "| --- | --- | --- | --- | --- |"])
        for row in val_rows:
            lines.append(f"| `{row[0]}` | {row[1]} | {row[2]} | {row[4]} | {row[7]} |")

    b_diag = getattr(snap, "bank_diagnostics", None)
    b_val = getattr(snap, "bank_valuation", None)
    b_disc = getattr(snap, "bank_disclosure", None)
    if b_diag is not None or b_val is not None:
        lines.extend(["", "## US Large Bank 5-Pillar Diagnostics & Gordon P/TBV"])
        if b_diag is not None:
            lines.append(f"- **Overall Bank Light**: `{b_diag.bank_quality_light.upper()}` (`{b_diag.bank_subtype}`) | **CET1 Buffer**: `+{b_diag.cet1_buffer_bps or 'n/a'} bps` | **HTM Loss / TCE**: `{b_diag.htm_loss_pct_of_tce or 'n/a'}%` | **Pro-Forma CET1 ex-HTM (24% tax)**: `{b_diag.pro_forma_cet1_ex_htm_pct or 'n/a'}%` | **Efficiency (GAAP)**: `{b_diag.efficiency_ratio_gaap_pct or 'n/a'}%` | **ROTCE**: `{b_diag.rotce_q_pct or 'n/a'}%`")
        if b_val is not None:
            lines.append(f"- **Valuation ({b_val.primary_tbvps_basis}, Ke={b_val.coe_pct}%, g={b_val.terminal_g_pct}%)**: **P/TBV Primary**: `{b_val.p_to_tbv_primary or 'n/a'}x` (XBRL-Std `{b_val.p_to_tbv_xbrl_std or 'n/a'}x`) | **Implied ROTCE**: `{b_val.implied_rotce_pct or 'n/a'}%` | **ROTCE Spread**: `{b_val.rotce_spread_vs_implied_pp or 'n/a'} pp` | **Signal**: `{b_val.valuation_signal}`")
            br = b_val.tbvps_bridge
            lines.append(f"- **TBVPS Bridge**: Reported `${br.tbvps_reported_usd or 'n/a'}` ({br.tbvps_reported_source}) vs XBRL-Std `${br.tbvps_xbrl_std_usd}` (gap `{br.tbvps_gap_pct or 'n/a'}%`, RSU add-on `{br.rsu_share_addon}`)")
            if b_val.sensitivity_matrix:
                lines.extend(["", "| Ke % | g % | Implied ROTCE % | Justified P/TBV | Spread (pp) |", "| --- | --- | --- | --- | --- |"])
                for cell in b_val.sensitivity_matrix:
                    lines.append(f"| {cell.coe_pct}% | {cell.terminal_g_pct}% | {cell.implied_rotce_pct or 'n/a'}% | {cell.justified_p_to_tbv or 'n/a'}x | {cell.rotce_spread_pp or 'n/a'} |")
        if b_disc is not None and b_disc.segments is not None:
            segs = b_disc.segments
            lines.extend(["", f"### 10-Q Segment Results (`{segs.r_file}`, Consolidated `{segs.consolidated_basis}`, Segments `{segs.segments_basis}`, Delta `${segs.reconciliation_delta_usd}` `{segs.reconciliation_status}`)", "", "| Segment | Basis | Net Revenue (USD) | Share % | PPNR (USD) | Net Income (USD) |", "| --- | --- | --- | --- | --- | --- |"])
            for sr in (*segs.operating_segments, *segs.corporate_or_other, *segs.reconciling_items):
                lines.append(f"| {sr.display_name} | `{sr.reporting_basis}` | {sr.net_revenue_usd or 'n/a'} | {sr.revenue_share_pct or '-'}% | {sr.ppnr_usd or 'n/a'} | {sr.net_income_usd or 'n/a'} |")

    lines.extend(["", "## Verified Evidence Cards & Citations"])
    if view.cards:
        for card in view.cards:
            lines.append("")
            lines.append(f"### `{card.card_id}` — {card.worker_role} / `{card.topic_tag}`")
            lines.append(f"- **Finding**: {card.finding}")
            if card.supporting_fact_ids:
                lines.append(f"- **Supporting Fact IDs**: {', '.join(f'`{fid}`' for fid in card.supporting_fact_ids)}")
            if card.caveat:
                lines.append(f"- **Caveat**: {card.caveat}")
            if card.citations:
                lines.append("- **Citations**:")
                for cit in card.citations:
                    loc = f" ({cit.location})" if cit.location else ""
                    lines.append(f"  - `{cit.source_id}`{loc}: \"{cit.quotation}\"")
            elif card.redacted:
                lines.append("- **Citations**: [REDACTED: non-exportable source]")
    else:
        lines.append("- No verified evidence cards recorded.")

    lines.extend(["", "## Adversarial Bear Cases"])
    if view.bear_cases:
        for bear in view.bear_cases:
            anchors = ", ".join(f"`{a}`" for a in bear.anchored_evidence_ids) or "[REDACTED]"
            lines.append(
                f"- **`{bear.bear_id}`** (`{bear.anchor_kind}`, severity=`{bear.severity}`, anchors={anchors}): "
                f"{bear.thesis_challenge} — **Next-Quarter Invalidation**: {bear.next_quarter_invalidation_trigger}"
            )
    else:
        lines.append("- No anchored bear cases recorded.")

    lines.extend(["", "## Open Gaps & Missing Evidence"])
    if dossier.open_gaps:
        for gap in dossier.open_gaps:
            worker = f" ({gap.worker_role})" if gap.worker_role else ""
            lines.append(f"- `[{gap.stage}]{worker} {gap.code}`: {gap.message}")
    else:
        lines.append("- None.")

    if snap.consensus_summary.status == "unavailable" or view.consensus_redacted:
        lines.extend(["", "## Consensus & Transcript Export Status"])
        if view.consensus_redacted:
            lines.append("- Consensus data withheld from export (`export=False`).")
        else:
            lines.append(f"- Consensus status: `{snap.consensus_summary.status}` ({snap.consensus_summary.unavailable_reason or 'n/a'}).")
        if view.transcript_redacted:
            lines.append("- Call transcript metadata/content withheld from export (`export=False`).")
        else:
            lines.append(f"- Transcript status: `{snap.transcript_index.status}` ({snap.transcript_index.unavailable_reason or 'n/a'}).")

    payload = "\n".join(lines).rstrip() + "\n"
    return DossierExportResult(
        dossier_id=dossier.dossier_id,
        dossier_sha256=dossier.dossier_sha256,
        format="markdown",
        report_mode=active_mode,
        filename=_safe_filename(dossier, "markdown"),
        media_type=EXPORT_MEDIA_TYPES["markdown"],
        content_bytes=payload.encode("utf-8"),
        exportable_unrestricted=(len(view.non_exportable_sources) == 0),
        redacted_sources=tuple(view.non_exportable_sources),
        redactions=tuple(view.redactions),
    )


# --------------------------------------------------------------------------- Format 2: CSV Export
def _csv_safe_cell(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, Decimal):
        return _fmt_decimal(val)
    text = str(val)
    if text and text[0] in ("=", "@", "\t", "\r"):
        return "'" + text
    return text


def export_dossier_csv(
    dossier: EarningsDossier,
    *,
    extra_source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> DossierExportResult:
    """Export structured financials, diagnostics, segments, guidance, and bear cases as UTF-8 CSV."""
    view = sanitize_dossier_for_export(dossier, extra_source_permissions=extra_source_permissions)
    snap = dossier.snapshot
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "section",
        "row_id",
        "period_or_category",
        "metric_or_topic",
        "value",
        "unit",
        "currency",
        "status",
        "qoq_or_secondary",
        "yoy_or_benchmark",
        "formula_or_threshold",
        "provenance_or_citations",
        "notes",
    ])

    def wrow(cells: Sequence[Any]) -> None:
        writer.writerow([_csv_safe_cell(c) for c in cells])

    # 1. Explicit Export Permissions & Redaction Notice rows first
    wrow([
        "export_notice",
        dossier.dossier_id,
        "permissions",
        "exportable_unrestricted",
        "false" if view.non_exportable_sources else "true",
        "boolean",
        "",
        "redacted" if view.non_exportable_sources else "unrestricted",
        ", ".join(view.non_exportable_sources),
        "",
        "Permissions.export",
        "",
        view.redaction_notice_lines[0],
    ])
    for idx, rec in enumerate(view.redactions, start=1):
        wrow([
            "export_redactions",
            f"redaction_{idx}",
            rec.source_id,
            rec.scope,
            "[REDACTED: export=False]",
            "",
            "",
            "redacted",
            "",
            "",
            "Permissions.export=False",
            rec.source_id,
            rec.reason,
        ])

    # 2. Metadata & Sector Gate
    sec_app = dossier.sector_applicability
    for key, value in (
        ("entity_id", dossier.entity_id),
        ("symbol", dossier.symbol or ""),
        ("cik", dossier.cik or ""),
        ("company_name", dossier.company_name or ""),
        ("period_label", dossier.period_label or ""),
        ("selected_l2_pack_id", dossier.selected_l2_pack_id or ""),
        ("sector_category", sec_app.sector_category),
        ("sector_is_gated", str(sec_app.is_gated).lower()),
        ("dossier_sha256", dossier.dossier_sha256),
    ):
        wrow(["metadata", dossier.dossier_id, "summary", key, value, "", "", "locked", "", "", "", "", ""])

    # 3. Valuation
    for vrow in _valuation_rows(view):
        wrow([
            "valuation",
            vrow[0],
            dossier.period_label or "",
            vrow[0],
            vrow[1],
            vrow[2],
            vrow[3],
            vrow[4],
            "",
            "",
            vrow[5],
            vrow[6],
            vrow[7],
        ])

    # 4. Quarterly Financials
    for stmt in snap.statements:
        for label, fname in _QUARTERLY_FIELDS:
            mv = getattr(stmt, fname, None)
            if mv is None:
                continue
            wrow([
                "quarterly_financials",
                f"{stmt.period_label}:{fname}",
                stmt.period_label,
                label,
                mv.value,
                mv.unit or stmt.monetary_unit or "",
                mv.currency or stmt.currency or "",
                "reported",
                stmt.period_type,
                stmt.period_end.isoformat() if stmt.period_end else "",
                mv.xbrl_tag or "",
                mv.fact_id or mv.source_id or "",
                "",
            ])

    # 5. Earnings Quality & R/Y/G Flags
    diag_rows, flag_rows = _diagnostics_rows(view)
    for drow in diag_rows:
        wrow([
            "earnings_quality",
            f"{drow[0]}:{drow[1]}",
            drow[0],
            drow[1],
            drow[2],
            drow[3],
            drow[4],
            drow[5],
            drow[6],
            drow[7],
            drow[8],
            drow[9],
            drow[10],
        ])
    for frow in flag_rows:
        wrow([
            "diagnostic_flags",
            frow[0],
            frow[2],
            frow[3],
            frow[4],
            frow[5],
            "",
            frow[1],
            "",
            "",
            frow[6],
            frow[8],
            frow[7],
        ])

    # 6. Segments & KPIs (segment worker cards + numeric claims + sanitized SEC tables)
    for card in view.cards:
        if card.worker_role == "worker_segments" or "segment" in card.topic_tag.casefold() or "kpi" in card.topic_tag.casefold():
            cits = "; ".join(f"{c.source_id}: {c.quotation}" for c in card.citations)
            wrow([
                "segments_and_kpis",
                card.card_id,
                card.topic_tag,
                card.worker_role,
                card.finding,
                "",
                "",
                "redacted" if card.redacted else "verified",
                ", ".join(card.supporting_fact_ids),
                "",
                "",
                cits,
                card.caveat or "",
            ])
            for idx, claim in enumerate(card.numeric_claims, start=1):
                wrow([
                    "segments_and_kpis_claim",
                    f"{card.card_id}:c{idx}",
                    card.topic_tag,
                    claim.get("metric") or claim.get("label") or claim.get("name") or f"claim_{idx}",
                    claim.get("value") if claim.get("value") is not None else str(claim),
                    claim.get("unit") or "",
                    claim.get("currency") or "",
                    "verified",
                    claim.get("period") or "",
                    claim.get("yoy") or "",
                    "",
                    ", ".join(card.supporting_fact_ids),
                    "",
                ])
    if snap.filing_table_catalog is not None:
        for entry in snap.filing_table_catalog.all_reports:
            wrow([
                "filing_table_catalog",
                entry.r_file,
                entry.normalized_category,
                entry.short_name,
                entry.long_name,
                "",
                "",
                "available",
                entry.menu_category,
                "",
                "",
                entry.url,
                "",
            ])

    # 7. Guidance & Non-GAAP
    guid_rows, bridge_rows, warn_rows = _guidance_rows(view)
    for grow in guid_rows:
        wrow([
            "guidance_vs_prior_consensus",
            grow[0],
            grow[1],
            grow[0],
            grow[2],
            grow[3],
            grow[4],
            f"prior={grow[8]}; consensus={grow[11]}",
            f"prior_mid={grow[6]}",
            f"consensus={grow[10]}",
            f"new_dir={grow[16]} ({grow[13]}..{grow[15]})",
            grow[18],
            "",
        ])
    for brow in bridge_rows:
        wrow([
            "nongaap_reconciliation",
            f"{brow[0]}:{brow[1]}",
            brow[1],
            brow[0],
            brow[5],
            brow[2],
            brow[3],
            brow[9],
            f"gaap={brow[4]}",
            f"uplift_pct={brow[10]}",
            brow[12],
            brow[13],
            f"recurring={brow[11]}",
        ])
    for wr in warn_rows:
        wrow([
            "nongaap_warning",
            wr[0],
            wr[4],
            wr[2],
            wr[5],
            "",
            "",
            wr[1],
            wr[3],
            "",
            "",
            wr[6],
            "",
        ])

    # 8. Bear Cases & All Evidence Cards & Open Gaps
    for bear in view.bear_cases:
        wrow([
            "bear_cases",
            bear.bear_id,
            bear.anchor_kind,
            ", ".join(bear.anchored_evidence_ids) or "[REDACTED]",
            bear.thesis_challenge,
            "",
            "",
            "redacted" if bear.redacted else bear.severity,
            bear.next_quarter_invalidation_trigger,
            ", ".join(bear.supporting_numbers),
            "",
            ", ".join(bear.anchored_evidence_ids),
            "",
        ])
    for card in view.cards:
        cits = "; ".join(f"{c.source_id}: {c.quotation}" for c in card.citations)
        wrow([
            "evidence_cards",
            card.card_id,
            card.worker_role,
            card.topic_tag,
            card.finding,
            "",
            "",
            "redacted" if card.redacted else "verified",
            ", ".join(card.supporting_fact_ids),
            "",
            "",
            cits,
            card.caveat or "",
        ])
    if any(g.code == "bank_disclosure_restatement" for g in dossier.open_gaps):
        wrow(["restatement", dossier.dossier_id, "", "", view.sanitized_memo_markdown])
    for gap in dossier.open_gaps:
        wrow([
            "open_gaps",
            gap.gap_id,
            gap.stage,
            gap.code,
            gap.message,
            "",
            "",
            "blocking" if gap.is_blocking else "open",
            gap.worker_role or "",
            "",
            "",
            gap.source or "",
            gap.fallback or "",
        ])

    content_bytes = buf.getvalue().encode("utf-8")
    return DossierExportResult(
        dossier_id=dossier.dossier_id,
        dossier_sha256=dossier.dossier_sha256,
        format="csv",
        report_mode=None,
        filename=_safe_filename(dossier, "csv"),
        media_type=EXPORT_MEDIA_TYPES["csv"],
        content_bytes=content_bytes,
        exportable_unrestricted=(len(view.non_exportable_sources) == 0),
        redacted_sources=tuple(view.non_exportable_sources),
        redactions=tuple(view.redactions),
    )


# --------------------------------------------------------------------------- Format 3: Multi-Sheet XLSX Export
def _sanitize_xml_text(raw: str) -> str:
    cleaned = _XML_INVALID_CHARS_RE.sub("", raw)
    if len(cleaned) > 32767:
        return cleaned[:32752] + "... [truncated]"
    return cleaned


def _col_letter(col_idx_1based: int) -> str:
    out: list[str] = []
    n = col_idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def _sanitize_sheet_title(title: str, used: set[str]) -> str:
    base = _SHEET_INVALID_CHARS_RE.sub("_", title).strip() or "Sheet"
    candidate = base[:31]
    idx = 2
    while candidate in used:
        suffix = f"_{idx}"
        candidate = base[: 31 - len(suffix)] + suffix
        idx += 1
    used.add(candidate)
    return candidate


def _build_xlsx_with_openpyxl(sheets: Sequence[tuple[str, Sequence[Sequence[Any]]]]) -> bytes:
    import openpyxl  # type: ignore[import-not-found]
    from openpyxl.styles import Font, PatternFill  # type: ignore[import-not-found]

    wb = openpyxl.Workbook()
    default_ws = wb.active
    used_titles: set[str] = set()
    header_font = Font(bold=True, color="111827")
    header_fill = PatternFill(fill_type="solid", start_color="D9E1F2", end_color="D9E1F2")
    section_font = Font(bold=True, color="1F4E78")

    for sheet_idx, (title, rows) in enumerate(sheets):
        safe_title = _sanitize_sheet_title(title, used_titles)
        ws = default_ws if sheet_idx == 0 else wb.create_sheet(title=safe_title)
        ws.title = safe_title
        for r_idx, row in enumerate(rows, start=1):
            is_section = len(row) > 0 and isinstance(row[0], str) and row[0].startswith("[SECTION]")
            is_header = len(row) > 0 and isinstance(row[0], str) and row[0].startswith("[HEADER]")
            for c_idx, val in enumerate(row, start=1):
                clean_val: Any = val
                if isinstance(val, str):
                    if c_idx == 1 and is_section:
                        clean_val = val.removeprefix("[SECTION]").strip()
                    elif c_idx == 1 and is_header:
                        clean_val = val.removeprefix("[HEADER]").strip()
                    clean_val = _sanitize_xml_text(clean_val)
                elif isinstance(val, Decimal):
                    clean_val = float(val) if val.is_finite() else ""
                cell = ws.cell(row=r_idx, column=c_idx, value=clean_val)
                if is_header:
                    cell.font = header_font
                    cell.fill = header_fill
                elif is_section:
                    cell.font = section_font

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _build_xlsx_stdlib(sheets: Sequence[tuple[str, Sequence[Sequence[Any]]]]) -> bytes:
    """Standards-compliant OpenXML .xlsx generator implemented strictly with Python stdlib."""
    shared_strings: list[str] = []
    shared_index: dict[str, int] = {}
    total_shared_refs = 0

    def intern(text: str) -> int:
        nonlocal total_shared_refs
        safe = _sanitize_xml_text(text)
        total_shared_refs += 1
        idx = shared_index.get(safe)
        if idx is None:
            idx = len(shared_strings)
            shared_strings.append(safe)
            shared_index[safe] = idx
        return idx

    used_titles: set[str] = set()
    sheet_entries: list[tuple[int, str, str]] = []

    for sheet_num, (raw_title, rows) in enumerate(sheets, start=1):
        safe_title = _sanitize_sheet_title(raw_title, used_titles)
        col_widths: dict[int, int] = {}
        row_xml_parts: list[str] = []

        for r_idx, row in enumerate(rows, start=1):
            first_str = row[0] if row and isinstance(row[0], str) else ""
            is_section = first_str.startswith("[SECTION]")
            is_header = first_str.startswith("[HEADER]")
            style_idx = 1 if is_header else (2 if is_section else 0)

            cell_parts: list[str] = []
            for c_idx, raw_val in enumerate(row, start=1):
                val = raw_val
                if c_idx == 1 and isinstance(val, str):
                    if is_section:
                        val = val.removeprefix("[SECTION]").strip()
                    elif is_header:
                        val = val.removeprefix("[HEADER]").strip()
                if val is None or val == "":
                    continue
                coord = f"{_col_letter(c_idx)}{r_idx}"
                if isinstance(val, bool):
                    s_idx = intern("TRUE" if val else "FALSE")
                    col_widths[c_idx] = max(col_widths.get(c_idx, 10), 6)
                    cell_parts.append(f'<c r="{coord}" s="{style_idx}" t="s"><v>{s_idx}</v></c>')
                elif isinstance(val, Decimal):
                    if not val.is_finite():
                        continue
                    num_text = _fmt_decimal(val)
                    col_widths[c_idx] = max(col_widths.get(c_idx, 10), min(42, len(num_text) + 2))
                    cell_parts.append(f'<c r="{coord}" s="{style_idx}" t="n"><v>{num_text}</v></c>')
                elif isinstance(val, (int, float)):
                    if isinstance(val, float) and not math.isfinite(val):
                        continue
                    num_text = str(val)
                    col_widths[c_idx] = max(col_widths.get(c_idx, 10), min(42, len(num_text) + 2))
                    cell_parts.append(f'<c r="{coord}" s="{style_idx}" t="n"><v>{num_text}</v></c>')
                else:
                    text = str(val)
                    s_idx = intern(text)
                    col_widths[c_idx] = max(col_widths.get(c_idx, 12), min(56, len(text) + 3))
                    cell_parts.append(f'<c r="{coord}" s="{style_idx}" t="s"><v>{s_idx}</v></c>')

            row_xml_parts.append(f'<row r="{r_idx}">{"".join(cell_parts)}</row>')

        cols_xml = ""
        if col_widths:
            col_defs = [
                f'<col min="{idx}" max="{idx}" width="{min(58, max(12, w))}" customWidth="1"/>'
                for idx, w in sorted(col_widths.items())
            ]
            cols_xml = f"<cols>{''.join(col_defs)}</cols>"

        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView workbookViewId="0"/></sheetViews>'
            '<sheetFormatPr defaultRowHeight="15"/>'
            f"{cols_xml}"
            f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
            "</worksheet>"
        )
        sheet_entries.append((sheet_num, safe_title, sheet_xml))

    si_parts = [
        f'<si><t xml:space="preserve">{xml_escape(s)}</t></si>'
        for s in shared_strings
    ]
    shared_strings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{total_shared_refs}" uniqueCount="{len(shared_strings)}">'
        f'{"".join(si_parts)}'
        "</sst>"
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="3">'
        '<font><sz val="11"/><color rgb="FF111827"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF111827"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><color rgb="FF1F4E78"/><name val="Calibri"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFD9E1F2"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment wrapText="1" vertical="center"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment vertical="center"/></xf>'
        '</cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>'
    )

    wb_sheets_xml = "".join(
        f'<sheet name="{xml_escape(title, {"\'": "&apos;", "\"": "&quot;"})}" sheetId="{num}" r:id="rId{num}"/>'
        for num, title, _ in sheet_entries
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{wb_sheets_xml}</sheets>"
        "</workbook>"
    )

    wb_rels_parts = [
        f'<Relationship Id="rId{num}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{num}.xml"/>'
        for num, _, _ in sheet_entries
    ]
    next_id = len(sheet_entries) + 1
    wb_rels_parts.append(
        f'<Relationship Id="rId{next_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    wb_rels_parts.append(
        f'<Relationship Id="rId{next_id + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    )
    workbook_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(wb_rels_parts)}'
        "</Relationships>"
    )

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        "</Relationships>"
    )

    ct_overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>',
    ]
    for num, _, _ in sheet_entries:
        ct_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{num}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{"".join(ct_overrides)}'
        "</Types>"
    )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        '<dc:title>ForesightNine Buy-Side Earnings Dossier</dc:title>'
        '<dc:creator>ForesightNine</dc:creator>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:modified>'
        "</cp:coreProperties>"
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Application>ForesightNine Research Exporter</Application>'
        "</Properties>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml.encode("utf-8"))
        zf.writestr("_rels/.rels", root_rels_xml.encode("utf-8"))
        zf.writestr("docProps/core.xml", core_xml.encode("utf-8"))
        zf.writestr("docProps/app.xml", app_xml.encode("utf-8"))
        zf.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml.encode("utf-8"))
        zf.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
        zf.writestr("xl/sharedStrings.xml", shared_strings_xml.encode("utf-8"))
        for num, _, sheet_xml in sheet_entries:
            zf.writestr(f"xl/worksheets/sheet{num}.xml", sheet_xml.encode("utf-8"))
    return out.getvalue()


def _build_xlsx_sheets(view: SanitizedDossierView) -> list[tuple[str, list[list[Any]]]]:
    dossier = view.dossier
    snap = dossier.snapshot
    sec_app = dossier.sector_applicability

    # ---- Sheet 1: Summary & Valuation
    s1: list[list[Any]] = [
        ["[SECTION] Buy-Side Earnings Dossier Summary"],
        ["[HEADER] Field", "Value", "Notes"],
        ["Dossier ID", dossier.dossier_id, "Locked server-gated dossier"],
        ["Run ID", dossier.run_id, ""],
        ["Entity ID", dossier.entity_id, ""],
        ["Symbol", dossier.symbol or "", ""],
        ["CIK", dossier.cik or "", ""],
        ["Company Name", dossier.company_name or "", ""],
        ["Period Label", dossier.period_label or "", ""],
        ["Selected L2 Method Pack", dossier.selected_l2_pack_id or "earnings_review (L1)", dossier.dispatch_plan.selected_l2_pack_reason if dossier.dispatch_plan else ""],
        ["Sector Category", sec_app.sector_category, f"SIC={sec_app.sic_code or 'N/A'}"],
        ["Sector Gated", "TRUE" if sec_app.is_gated else "FALSE", sec_app.explanation],
        ["Dossier Digest (SHA-256)", dossier.dossier_sha256, ""],
        ["Deep Report Available", "TRUE" if view.sanitized_deep_report_markdown is not None else "FALSE", ""],
        [],
        ["[SECTION] Export Permissions & Redaction Notice"],
        ["[HEADER] Source ID / Status", "Scope", "Detail"],
        ["exportable_unrestricted", "summary", "FALSE" if view.non_exportable_sources else "TRUE"],
        ["non_exportable_sources", "summary", ", ".join(view.non_exportable_sources) or "None"],
        ["policy_notice", "export_permissions", view.redaction_notice_lines[0]],
    ]
    if view.redactions:
        for rec in view.redactions:
            s1.append([rec.source_id, rec.scope, rec.reason])
    else:
        s1.append(["all_sources", "all_sheets", "All referenced sources permit export (export=True); 0 redactions applied."])

    if sec_app.disabled_metrics or sec_app.recommended_alternatives:
        s1.extend([
            [],
            ["[SECTION] Sector Gate Details"],
            ["[HEADER] Kind", "Items"],
            ["Disabled Non-Financial Metrics", ", ".join(sec_app.disabled_metrics)],
            ["Recommended Sector Alternatives", ", ".join(sec_app.recommended_alternatives)],
        ])

    s1.extend([
        [],
        ["[SECTION] Valuation & Implied Expectations"],
        ["[HEADER] Metric", "Value", "Unit", "Currency", "Status", "Formula", "Input / Source IDs", "Notes"],
    ])
    val_rows = _valuation_rows(view)
    if val_rows:
        s1.extend(val_rows)
    else:
        s1.append(["valuation", "", "", "", "unavailable", "", "", "No valuation metrics computed in Stage 0 snapshot."])

    if snap.valuation is not None and snap.valuation.reverse_dcf.sensitivity_matrix:
        rdcf = snap.valuation.reverse_dcf
        s1.extend([[], ["[SECTION] Reverse-DCF Implied 5Y FCF CAGR (%) Sensitivity (WACC x Terminal Growth)"]])
        s1.append(["[HEADER] WACC % \\ Terminal g %", *[f"g={_fmt_decimal(g)}%" for g in rdcf.terminal_growth_axis_pct]])
        for wacc_pct, row_cells in zip(rdcf.wacc_axis_pct, rdcf.sensitivity_matrix):
            s1.append([
                f"WACC={_fmt_decimal(wacc_pct)}%",
                *[_num_or_str(cell.implied_5y_fcf_cagr_pct) if cell.status == "computed" else cell.status for cell in row_cells],
            ])

    # ---- Sheet 2: Quarterly Financials
    stmts: Sequence[PeriodFinancials] = snap.statements
    s2: list[list[Any]] = [
        ["[SECTION] Quarterly & Trailing Financials (Reported / Derived)"],
        [
            "[HEADER] Line Item",
            "Metric Key",
            "Unit / Currency",
            *[f"{p.period_label} ({p.period_type})" for p in stmts],
            "Latest Fact / Source ID",
        ],
    ]
    if stmts:
        for label, fname in _QUARTERLY_FIELDS:
            values = [getattr(p, fname, None) for p in stmts]
            if all(v is None for v in values):
                continue
            first_mv = next(v for v in values if v is not None)
            unit_label = " / ".join(x for x in (first_mv.unit or stmts[0].monetary_unit, first_mv.currency or stmts[0].currency) if x)
            prov = first_mv.fact_id or first_mv.source_id or ""
            s2.append([
                label,
                fname,
                unit_label,
                *[_num_or_str(v.value) if v is not None else "" for v in values],
                prov,
            ])
    else:
        s2.append(["No PeriodFinancials statements available in Stage 0 snapshot.", "", "", ""])

    # ---- Sheet 3: Earnings Quality
    diag_rows, flag_rows = _diagnostics_rows(view)
    s3: list[list[Any]] = [
        ["[SECTION] Earnings Quality & Margin / Cash Conversion Diagnostics"],
        ["[HEADER] Group", "Metric", "Value", "Unit", "Currency", "Status", "QoQ Delta", "YoY Delta", "Formula", "Input IDs", "Reason / Notes"],
    ]
    if diag_rows:
        s3.extend(diag_rows)
    else:
        s3.append(["diagnostics", "unavailable", "", "", "", "unavailable", "", "", "", "", "No earnings_quality_diagnostics result recorded."])

    s3.extend([
        [],
        ["[SECTION] R/Y/G Diagnostic Flags"],
        ["[HEADER] Flag ID", "Severity", "Group", "Metric Name", "Observed Value", "Unit", "Threshold Rule", "Summary", "Input IDs"],
    ])
    if flag_rows:
        s3.extend(flag_rows)
    else:
        s3.append(["none", "GREEN", "all", "", "", "", "", "No diagnostic flags triggered.", ""])

    # ---- Sheet 4: Segments & KPIs
    s4: list[list[Any]] = [
        ["[SECTION] Segments, Revenue Disaggregation & Industry KPIs"],
        ["[HEADER] Pack / Role", "Selected ID / Topic Tag", "Status / Finding", "Numeric Claims / Details", "Fact IDs", "Citations", "Caveat"],
        [
            "L2 Method Pack",
            dossier.selected_l2_pack_id or "earnings_review (L1)",
            dossier.dispatch_plan.selected_l2_pack_reason if dossier.dispatch_plan else "Default L1 backbone",
            "",
            "",
            "",
            "",
        ],
    ]
    seg_cards = [c for c in view.cards if c.worker_role == "worker_segments" or "segment" in c.topic_tag.casefold() or "kpi" in c.topic_tag.casefold()]
    for card in seg_cards:
        claims_summary = "; ".join(
            f"{cl.get('metric') or cl.get('label') or 'metric'}={cl.get('value')}"
            for cl in card.numeric_claims
        )
        cits = "; ".join(f"{c.source_id}: {c.quotation}" for c in card.citations)
        s4.append([
            card.card_id,
            card.topic_tag,
            card.finding,
            claims_summary,
            ", ".join(card.supporting_fact_ids),
            cits,
            card.caveat or "",
        ])

    if snap.earnings_release is not None:
        for role_name, rel_period in (("current", snap.earnings_release.current), ("prior", snap.earnings_release.prior)):
            if rel_period and rel_period.ex99_1 and rel_period.ex99_1.tables:
                s4.extend([[], [f"[SECTION] SEC 8-K EX-99.1 Tables ({role_name}: {rel_period.accession})"]])
                for tbl_idx, tbl in enumerate(rel_period.ex99_1.tables[:6], start=1):
                    s4.append([f"[HEADER] Table {tbl_idx}: {tbl.title or 'Untitled'}", *tbl.headers])
                    for trow in tbl.rows[:25]:
                        s4.append(["", *trow])

    if snap.filing_table_catalog is not None and snap.filing_table_catalog.all_reports:
        s4.extend([
            [],
            ["[SECTION] FilingSummary Table Catalog"],
            ["[HEADER] R-File", "Category", "Short Name", "Long Name", "SEC URL"],
        ])
        for rep in snap.filing_table_catalog.all_reports[:40]:
            s4.append([rep.r_file, rep.normalized_category, rep.short_name, rep.long_name, rep.url])

    # ---- Sheet 5: Guidance & Non-GAAP
    guid_rows, bridge_rows, warn_rows = _guidance_rows(view)
    s5: list[list[Any]] = [
        ["[SECTION] Guidance vs. Prior Guidance & Consensus"],
        [
            "[HEADER] Metric", "Period", "Actual", "Unit", "Currency",
            "Prior Low", "Prior Mid", "Prior High", "vs Prior Outcome", "vs Prior %",
            "Consensus Benchmark", "vs Consensus Outcome", "vs Consensus %",
            "New Low", "New Mid", "New High", "New Direction", "New vs Prior %", "Input IDs",
        ],
    ]
    if guid_rows:
        s5.extend(guid_rows)
    else:
        s5.append(["No structured guidance comparisons recorded.", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])

    s5.extend([
        [],
        ["[SECTION] Non-GAAP Bridge Reconciliations"],
        [
            "[HEADER] Metric", "Period", "Unit", "Currency", "GAAP Value", "Reported Non-GAAP",
            "Adjustments Sum", "Computed Non-GAAP", "Discrepancy", "Tie Status",
            "Uplift %", "Recurring Adjustments", "Formula", "Input IDs",
        ],
    ])
    if bridge_rows:
        s5.extend(bridge_rows)
    else:
        s5.append(["No Non-GAAP reconciliations recorded.", "", "", "", "", "", "", "", "", "", "", "", "", ""])

    s5.extend([
        [],
        ["[SECTION] Non-GAAP & Guidance Warning Flags"],
        ["[HEADER] Flag ID", "Severity", "Metric", "Adjustment", "Category", "Summary", "Input IDs"],
    ])
    if warn_rows:
        s5.extend(warn_rows)
    else:
        s5.append(["none", "GREEN", "", "", "", "No Non-GAAP adjustment warnings triggered.", ""])

    s5.extend([
        [],
        ["[SECTION] Consensus & Call Transcript Export Status"],
        ["[HEADER] Source", "Status", "Details"],
        [
            snap.consensus_summary.source_id or "consensus",
            "REDACTED (export=False)" if view.consensus_redacted else snap.consensus_summary.status,
            "[REDACTED: third-party/BYOK consensus excluded per export=False]"
            if view.consensus_redacted
            else f"{len(snap.consensus_summary.records)} record(s)",
        ],
        [
            snap.transcript_index.source_id or "transcript",
            "REDACTED (export=False)" if view.transcript_redacted else snap.transcript_index.status,
            "[REDACTED: transcript speakers/sections excluded per export=False]"
            if view.transcript_redacted
            else (
                f"Sections={', '.join(snap.transcript_index.sections)}; Speakers={', '.join(snap.transcript_index.speakers)}"
                if snap.transcript_index.status == "available"
                else (snap.transcript_index.unavailable_reason or "unavailable")
            ),
        ],
    ])

    # ---- Sheet 6: Bear Case & Sources
    s6: list[list[Any]] = [
        ["[SECTION] Adversarial Bear Cases (Anchored)"],
        ["[HEADER] Bear ID", "Severity", "Anchor Kind", "Anchored Evidence IDs", "Thesis Challenge", "Next-Quarter Invalidation Trigger", "Supporting Numbers"],
    ]
    if view.bear_cases:
        for bear in view.bear_cases:
            s6.append([
                bear.bear_id,
                bear.severity,
                bear.anchor_kind,
                ", ".join(bear.anchored_evidence_ids) or "[REDACTED]",
                bear.thesis_challenge,
                bear.next_quarter_invalidation_trigger,
                "; ".join(bear.supporting_numbers),
            ])
    else:
        s6.append(["none", "", "", "", "No anchored bear cases recorded.", "", ""])

    if dossier.rejected_bear_cases:
        s6.extend([
            [],
            ["[SECTION] Rejected Generic/Unanchored Bear Cases (Server Gate Audit)"],
            ["[HEADER] Bear ID", "Anchor Kind", "Anchored IDs", "Rejection Reason"],
        ])
        for rej in dossier.rejected_bear_cases:
            s6.append([
                rej.bear_id,
                rej.anchor_kind or "",
                ", ".join(rej.anchored_evidence_ids),
                rej.rejection_reason,
            ])

    s6.extend([
        [],
        ["[SECTION] All Verified Evidence Cards & Citations"],
        ["[HEADER] Card ID", "Worker Role", "Topic Tag", "Finding", "Supporting Fact IDs", "Citations", "Caveat"],
    ])
    for card in view.cards:
        cits = "; ".join(f"{c.source_id}: {c.quotation}" for c in card.citations)
        s6.append([
            card.card_id,
            card.worker_role,
            card.topic_tag,
            card.finding,
            ", ".join(card.supporting_fact_ids),
            cits,
            card.caveat or "",
        ])

    s6.extend([
        [],
        ["[SECTION] Source Provenance & Export Permissions"],
        ["[HEADER] Source ID", "Title", "Kind", "Retrieval Method", "Export Allowed", "SHA-256", "URL"],
    ])
    for ref in snap.prefetched_sources:
        exp_ok = (ref.permissions.get("export", True) is not False) and (ref.source_id not in view.non_exportable_sources)
        s6.append([
            ref.source_id,
            "[REDACTED: export=False]" if not exp_ok else ref.title,
            ref.kind,
            ref.retrieval_method,
            "TRUE" if exp_ok else "FALSE",
            ref.content_sha256,
            "" if not exp_ok else (ref.url or ""),
        ])

    s6.extend([
        [],
        ["[SECTION] Open Gaps"],
        ["[HEADER] Gap ID", "Stage", "Worker Role", "Code", "Message", "Fallback"],
    ])
    if any(g.code == "bank_disclosure_restatement" for g in dossier.open_gaps):
        s6.append([dossier.dossier_id, "disclosure_review", "", "restatement", view.sanitized_memo_markdown, ""])
    if dossier.open_gaps:
        for gap in dossier.open_gaps:
            s6.append([gap.gap_id, gap.stage, gap.worker_role or "", gap.code, gap.message, gap.fallback or ""])
    else:
        s6.append(["none", "", "", "", "No open gaps.", ""])

    s6.extend([
        [],
        ["[SECTION] Export Permissions & Redaction Notice"],
        ["[HEADER] Source ID", "Scope", "Detail"],
        ["policy_notice", "export_permissions", view.redaction_notice_lines[0]],
    ])
    if view.redactions:
        for rec in view.redactions:
            s6.append([rec.source_id, rec.scope, rec.reason])
    else:
        s6.append(["all_sources", "all_sheets", "All referenced sources permit export (export=True); 0 redactions applied."])

    return [
        (XLSX_SHEET_NAMES[0], s1),
        (XLSX_SHEET_NAMES[1], s2),
        (XLSX_SHEET_NAMES[2], s3),
        (XLSX_SHEET_NAMES[3], s4),
        (XLSX_SHEET_NAMES[4], s5),
        (XLSX_SHEET_NAMES[5], s6),
    ]


def export_dossier_xlsx(
    dossier: EarningsDossier,
    *,
    extra_source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> DossierExportResult:
    """Export a locked ``EarningsDossier`` as a 6-sheet OpenXML ``.xlsx`` workbook."""
    view = sanitize_dossier_for_export(dossier, extra_source_permissions=extra_source_permissions)
    sheets = _build_xlsx_sheets(view)
    try:
        import openpyxl  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        workbook_bytes = _build_xlsx_stdlib(sheets)
    else:
        workbook_bytes = _build_xlsx_with_openpyxl(sheets)

    return DossierExportResult(
        dossier_id=dossier.dossier_id,
        dossier_sha256=dossier.dossier_sha256,
        format="xlsx",
        report_mode=None,
        filename=_safe_filename(dossier, "xlsx"),
        media_type=EXPORT_MEDIA_TYPES["xlsx"],
        content_bytes=workbook_bytes,
        exportable_unrestricted=(len(view.non_exportable_sources) == 0),
        redacted_sources=tuple(view.non_exportable_sources),
        redactions=tuple(view.redactions),
    )


def export_earnings_dossier(
    dossier: EarningsDossier,
    *,
    fmt: ExportFormat,
    report_mode: MarkdownReportMode = "auto",
    extra_source_permissions: Mapping[str, Mapping[str, bool]] | None = None,
) -> DossierExportResult:
    """Dispatch export of a locked ``EarningsDossier`` to ``markdown``, ``csv``, or ``xlsx``."""
    if not dossier.locked:
        raise ExportUnavailableError("Only locked EarningsDossier artifacts can be exported.")
    if fmt == "markdown":
        return export_dossier_markdown(
            dossier,
            report_mode=report_mode,
            extra_source_permissions=extra_source_permissions,
        )
    if fmt == "csv":
        return export_dossier_csv(dossier, extra_source_permissions=extra_source_permissions)
    if fmt == "xlsx":
        return export_dossier_xlsx(dossier, extra_source_permissions=extra_source_permissions)
    raise ValueError(f"Unsupported export format: {fmt!r}")


__all__ = [
    "DossierExportResult",
    "EXPORT_EXTENSIONS",
    "EXPORT_MEDIA_TYPES",
    "ExportFormat",
    "ExportRedactionRecord",
    "ExportUnavailableError",
    "MarkdownReportMode",
    "SanitizedDossierView",
    "XLSX_SHEET_NAMES",
    "export_dossier_csv",
    "export_dossier_markdown",
    "export_dossier_xlsx",
    "export_earnings_dossier",
    "sanitize_dossier_for_export",
]
