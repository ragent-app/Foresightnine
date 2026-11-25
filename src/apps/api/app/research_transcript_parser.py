"""Layer 0 multi-format transcript parser, 4-tier authority classifier, and external V3a rules loader.

Pure 0-LLM (<1.5s, <25MB peak RAM):
- Loads and validates ``v3a_call_verification_rules.json`` (``extra="forbid"``) with
  separate ``context_budget_version`` and ``evaluation_rules_version`` + ``rules_sha256``.
- Cleans ``.vtt`` / ``.srt`` captions with rolling-line deduplication while preserving timestamps.
- Classifies document archetypes with explicit negative filters (10-K/10-Q, legal contracts,
  macro reports) so regular Topic uploads never falsely trigger V3a audits.
- Parses 6 multilingual transcript formats while preserving exact raw character offsets
  (``raw_char_start``, ``raw_char_end``) on untouched ``source.text`` via non-destructive
  ``boilerplate_spans`` masking.
- Protects the opening 1,000-char basis declaration (``call_default_basis``), extracts fiscal
  period ``(FY, FQ, call_date)``, pairs analyst Q&A exchanges (including follow-ups), cross-anchors
  spoken slide references (``"turning to slide 14"``), normalizes non-SEC entity keys via
  ``fin_data/cn.py`` + account alias map, and computes 0-LLM linguistic evasion priors.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .research_documents import page_for_offset
from .research_retrieval import tokenize_query

PARSER_VERSION = "v1"
DEFAULT_V3A_RULES_PATH = Path(__file__).resolve().parent / "fin_data" / "adapters" / "v3a_call_verification_rules.json"

DocumentArchetype = Literal[
    "transcript",
    "ir_slide_deck",
    "sellside_note",
    "form_10k_10q",
    "legal_contract",
    "macro_report",
    "other",
]
AuthorityTier = Literal[
    "sec_periodic_filing",
    "sec_earnings_release",
    "management_first_party",
    "third_party_opinion",
    "unknown",
]
CallDefaultBasis = Literal["non_gaap", "gaap", "unspecified"]
SpeakerRole = Literal["ceo", "cfo", "executive", "analyst", "operator", "unknown"]


# --------------------------------------------------------------------------- External Rules Schema

class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParsingRulesConfig(_StrictModel):
    negative_archetype_patterns: dict[str, list[str]]
    transcript_positive_patterns: list[str]
    sellside_positive_patterns: list[str]
    sellside_preview_patterns: list[str]
    sellside_review_patterns: list[str]
    qa_section_headers: list[str]
    safe_harbor_patterns: list[str]
    call_default_nongaap_patterns: list[str]
    call_default_gaap_patterns: list[str]
    executive_title_patterns: list[str]
    cfo_title_patterns: list[str]
    ceo_title_patterns: list[str]
    slide_reference_patterns: list[str]


class EvasionScoringConfig(_StrictModel):
    prior_weights: dict[str, float]
    quantitative_question_patterns: list[str]
    hedge_lexicon: list[str]
    confidentiality_whitelist_patterns: list[str]
    red_evasion_count_threshold: int = 2
    yellow_evasion_count_threshold: int = 1


class LedgerCollisionThresholds(_StrictModel):
    ar_minus_rev_pp_yellow: float = 8.0
    ar_minus_rev_pp_red: float = 15.0
    inv_minus_cogs_pp_yellow: float = 10.0
    inv_minus_cogs_pp_red: float = 20.0
    rpo_vs_rev_pp_yellow: float = -10.0
    rpo_vs_rev_pp_red: float = -20.0
    ocf_to_ni_yellow: float = 0.80
    ocf_to_ni_red: float = 0.60
    nongaap_margin_gap_bps_yellow: float = 200.0
    nongaap_margin_gap_bps_red: float = 500.0
    capex_yoy_pct_yellow: float = 30.0
    capex_yoy_pct_red: float = 60.0
    purchase_commitment_yoy_pct_yellow: float = 25.0
    purchase_commitment_yoy_pct_red: float = 50.0
    numeric_tolerance_rel_pct: float = 2.0
    margin_tolerance_bps: float = 25.0
    restatement_notice_pct: float = 1.0
    omission_entity_coverage_floor: float = 0.15
    speaker_baseline_shift_min_quarters: int = 2
    speaker_hedge_shift_yellow: float = 0.015
    speaker_evasion_rate_shift_yellow: float = 15.0


class ContextBudgetConfig(_StrictModel):
    max_total_chars: int = 32000
    dynamic_user_prefix_chars: int = 2500
    zone_b_qa_chars: int = 16000
    zone_b_evasion_bucket_ratio: float = 0.65
    zone_b_quant_bucket_ratio: float = 0.35
    zone_c_remarks_chars: int = 10000
    zone_d_aux_summary_chars: int = 2000
    map_doc_max_chars: int = 18000
    sellside_max_chars: int = 8000
    omission_phase2_max_chars: int = 8000
    map_reduce_qa_trigger_chars: int = 28000
    max_llm_calls_per_audit: int = 3
    remarks_8k_jaccard_dedup_threshold: float = 0.75
    opening_basis_protect_chars: int = 1000


class RadarAlertThresholds(_StrictModel):
    min_contradiction_severity: Literal["yellow", "red"] = "yellow"
    alert_on_commitment_miss: bool = True
    alert_on_silent_drop_high_risk: bool = True
    alert_on_item1a_omission: bool = True
    alert_on_late_notice: bool = True
    check_filings_cooldown_minutes: int = 30
    check_filings_max_ciks_per_call: int = 5


class V3aCallVerificationRules(_StrictModel):
    schema_version: Literal["1"] = "1"
    context_budget_version: str
    evaluation_rules_version: str
    parsing_rules: ParsingRulesConfig
    evasion_scoring: EvasionScoringConfig
    ledger_collision_thresholds: LedgerCollisionThresholds
    item1a_risk_buckets: dict[str, list[str]]
    negation_context_markers: list[str]
    context_budget: ContextBudgetConfig
    radar_alert_thresholds: RadarAlertThresholds


class CompiledV3aRules:
    """Loaded rules paired with precompiled regexes and deterministic 16-char ``rules_sha256``."""

    def __init__(self, rules: V3aCallVerificationRules, raw_sha256: str) -> None:
        self.rules = rules
        self.rules_sha256 = raw_sha256[:16]
        self.context_budget_version = rules.context_budget_version
        self.evaluation_rules_version = rules.evaluation_rules_version

        pr = rules.parsing_rules
        self.negative_re = {
            k: [re.compile(pat, re.IGNORECASE) for pat in pats]
            for k, pats in pr.negative_archetype_patterns.items()
        }
        self.transcript_pos_re = [re.compile(p, re.IGNORECASE) for p in pr.transcript_positive_patterns]
        self.sellside_pos_re = [re.compile(p, re.IGNORECASE) for p in pr.sellside_positive_patterns]
        self.sellside_preview_re = [re.compile(p, re.IGNORECASE) for p in pr.sellside_preview_patterns]
        self.sellside_review_re = [re.compile(p, re.IGNORECASE) for p in pr.sellside_review_patterns]
        self.qa_header_re = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in pr.qa_section_headers]
        self.safe_harbor_re = [re.compile(p, re.IGNORECASE) for p in pr.safe_harbor_patterns]
        self.default_nongaap_re = [re.compile(p, re.IGNORECASE) for p in pr.call_default_nongaap_patterns]
        self.default_gaap_re = [re.compile(p, re.IGNORECASE) for p in pr.call_default_gaap_patterns]
        self.exec_title_re = [re.compile(p, re.IGNORECASE) for p in pr.executive_title_patterns]
        self.cfo_title_re = [re.compile(p, re.IGNORECASE) for p in pr.cfo_title_patterns]
        self.ceo_title_re = [re.compile(p, re.IGNORECASE) for p in pr.ceo_title_patterns]
        self.slide_ref_re = [re.compile(p, re.IGNORECASE) for p in pr.slide_reference_patterns]

        es = rules.evasion_scoring
        self.quant_q_re = [re.compile(p, re.IGNORECASE) for p in es.quantitative_question_patterns]
        self.conf_whitelist_re = [re.compile(p, re.IGNORECASE) for p in es.confidentiality_whitelist_patterns]
        self.hedge_terms = tuple(h.casefold() for h in es.hedge_lexicon)


@lru_cache(maxsize=8)
def _load_v3a_rules_cached(path_str: str, mtime_ns: int) -> CompiledV3aRules:
    del mtime_ns
    raw_bytes = Path(path_str).read_bytes()
    digest = sha256(raw_bytes).hexdigest()
    data = json.loads(raw_bytes.decode("utf-8"))
    parsed = V3aCallVerificationRules.model_validate(data)
    return CompiledV3aRules(parsed, digest)


def load_v3a_call_rules(path: str | Path | None = None) -> CompiledV3aRules:
    """Load and hot-cache ``v3a_call_verification_rules.json`` keyed by file mtime."""
    target = Path(path) if path is not None else DEFAULT_V3A_RULES_PATH
    st = target.stat()
    return _load_v3a_rules_cached(str(target.resolve()), st.st_mtime_ns)


# --------------------------------------------------------------------------- Parsed Layer 0 Models

class SpeakerMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: SpeakerRole = "unknown"
    title: str | None = None
    firm: str | None = None


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_index: int
    section: Literal["prepared_remarks", "qa", "operator", "unstructured"] = "prepared_remarks"
    speaker_name: str
    speaker_role: SpeakerRole = "unknown"
    speaker_firm: str | None = None
    text: str
    raw_char_start: int
    raw_char_end: int
    page_start: int | None = None
    page_end: int | None = None
    timestamp_start: str | None = None
    slide_references: list[int] = Field(default_factory=list)


class QaExchange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchange_index: int
    analyst_name: str
    analyst_firm: str | None = None
    executive_names: list[str] = Field(default_factory=list)
    executive_roles: list[SpeakerRole] = Field(default_factory=list)
    question_text: str
    answer_text: str
    followup_count: int = 0
    raw_char_start: int
    raw_char_end: int
    question_raw_start: int
    question_raw_end: int
    answer_raw_start: int
    answer_raw_end: int
    page_start: int | None = None
    page_end: int | None = None
    timestamp_start: str | None = None
    slide_references: list[int] = Field(default_factory=list)
    # 0-LLM linguistic evasion & priority priors
    asked_quantitative: bool = False
    answered_quantitative: bool = False
    entity_overlap_ratio: float = 1.0
    hedge_density: float = 0.0
    verbosity_ratio: float = 1.0
    numeric_guidance_density: float = 0.0
    multi_part_question_hit: bool = False
    analyst_cluster_topic_hit: bool = False
    thesis_or_flag_hit: bool = False
    evasion_prior_score: float = 0.0
    topic_keywords: list[str] = Field(default_factory=list)


class ArchetypeClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archetype: DocumentArchetype
    authority_tier: AuthorityTier
    confidence: float = 0.0
    strong_feature_count: int = 0
    auto_trigger_eligible: bool = False
    sellside_timing: Literal["pre_earnings_preview", "post_earnings_review", "unspecified"] | None = None
    negative_filter_hit: str | None = None
    reasons: list[str] = Field(default_factory=list)


class ExecutiveBaselineStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executive_name: str
    speaker_role: SpeakerRole = "executive"
    answer_count: int = 0
    avg_hedge_density: float = 0.0
    avg_verbosity_ratio: float = 1.0
    avg_entity_overlap: float = 1.0


class ParsedCallDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1"] = "1"
    parser_version: str = PARSER_VERSION
    owner_id: str | None = None
    source_id: str
    content_sha256: str
    title: str
    retrieval_method: str = "user_supplied"
    classification: ArchetypeClassification
    structure_mode: Literal["structured_transcript", "slide_deck", "sellside_note", "unstructured_fallback"] = "structured_transcript"
    call_default_basis: CallDefaultBasis = "unspecified"
    detected_symbol: str | None = None
    detected_company_name: str | None = None
    call_fiscal_year: int | None = None
    call_fiscal_quarter: int | None = None
    call_date_iso: str | None = None
    is_subtitle_source: bool = False
    boilerplate_spans: list[tuple[int, int]] = Field(default_factory=list)
    speaker_directory: dict[str, SpeakerMeta] = Field(default_factory=dict)
    turns: list[TranscriptTurn] = Field(default_factory=list)
    qa_exchanges: list[QaExchange] = Field(default_factory=list)
    slide_references_map: dict[int, list[int]] = Field(default_factory=dict)  # slide_num -> [turn_indices]
    executive_baselines: list[ExecutiveBaselineStats] = Field(default_factory=list)
    open_gaps: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- VTT / SRT Subtitle Cleaner

_VTT_TIME_RE = re.compile(
    r"^\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,]\d{3}\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,]\d{3}.*$"
)
_VTT_VOICE_TAG_RE = re.compile(r"<v\s+([^>]+)>", re.IGNORECASE)
_HTML_INLINE_TAG_RE = re.compile(r"<[^>]+>")


def is_subtitle_text(text: str) -> bool:
    head = (text or "")[:2000]
    if head.lstrip("\ufeff").startswith("WEBVTT"):
        return True
    return len(_VTT_TIME_RE.findall(head)) >= 2 or bool(re.search(r"^\s*1\s*\r?\n\s*\d{2}:\d{2}:\d{2},\d{3}\s*-->", head))


def clean_subtitle_text_with_timestamps(raw_text: str) -> tuple[str, list[tuple[int, str]]]:
    """Strip VTT/SRT headers & cue numbers with rolling-line dedup, returning ``(clean_text, [(char_offset, HH:MM:SS)])``."""
    lines = (raw_text or "").replace("\r\n", "\n").splitlines()
    out_lines: list[str] = []
    ts_anchors: list[tuple[int, str]] = []
    current_ts: str | None = None
    prev_norm_lines: list[str] = []
    char_pos = 0

    for ln in lines:
        s = ln.strip()
        if not s or s.upper() == "WEBVTT" or s.startswith(("Kind:", "Language:", "NOTE ")):
            continue
        if s.isdigit():
            continue
        m_ts = _VTT_TIME_RE.match(s)
        if m_ts:
            hh = (m_ts.group(1) or "00").zfill(2)
            mm = m_ts.group(2)
            ss = m_ts.group(3)
            current_ts = f"{hh}:{mm}:{ss}"
            continue
        # Convert <v Speaker Name>Text</v> into "Speaker Name: Text"
        voice = _VTT_VOICE_TAG_RE.search(s)
        speaker_prefix = ""
        if voice:
            speaker_prefix = f"{voice.group(1).strip()}: "
        cleaned = _HTML_INLINE_TAG_RE.sub("", s).strip()
        if not cleaned:
            continue
        if speaker_prefix and not cleaned.startswith(speaker_prefix):
            cleaned = speaker_prefix + cleaned

        norm_key = re.sub(r"\s+", " ", cleaned).casefold()
        # Rolling-line dedup (YouTube/Zoom live captions repeat previous line as top line of next cue)
        if prev_norm_lines and norm_key in prev_norm_lines[-3:]:
            continue
        # Strip overlap prefix if new cue repeats the entire tail of the previous line
        if out_lines:
            last_low = out_lines[-1].casefold()
            if norm_key.startswith(last_low) and len(norm_key) > len(last_low) + 2:
                delta = cleaned[len(out_lines[-1]) :].strip()
                if not delta:
                    continue
                cleaned = delta
                norm_key = re.sub(r"\s+", " ", cleaned).casefold()

        if current_ts is not None:
            ts_anchors.append((char_pos, current_ts))
            current_ts = None
        out_lines.append(cleaned)
        prev_norm_lines.append(norm_key)
        char_pos += len(cleaned) + 1

    return "\n".join(out_lines), ts_anchors


# --------------------------------------------------------------------------- Non-SEC Entity Normalization (D1)

_SLUG_CLEAN_RE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def normalize_entity_key(
    *,
    cik: str | None = None,
    symbol: str | None = None,
    company_name: str | None = None,
    alias_map: Mapping[str, str] | None = None,
) -> str:
    """Resolve canonical storage key: ``cik10`` for SEC issuers, else ``nonsec:<exchange:code|slug>``."""
    if cik and str(cik).strip().isdigit():
        return str(int(str(cik).strip())).zfill(10)

    # Check account-scoped alias table first
    candidates = [v.strip() for v in (symbol, company_name) if isinstance(v, str) and v.strip()]
    if alias_map:
        for cand in candidates:
            hit = alias_map.get(cand.casefold()) or alias_map.get(cand)
            if isinstance(hit, str) and hit.strip():
                return hit.strip()

    # Try CN / HK code canonicalization via fin_data/cn.py
    from .fin_data.cn import parse_cn_code, parse_hk_code

    for cand in candidates:
        cn_hit = parse_cn_code(cand)
        if cn_hit is not None:
            return f"nonsec:{cn_hit.entity_id}"
        hk_hit = parse_hk_code(cand)
        if hk_hit is not None:
            return f"nonsec:{hk_hit.entity_id}"

    base = (candidates[0] if candidates else "unknown").casefold()
    slug = _SLUG_CLEAN_RE.sub("-", base).strip("-")[:64] or "unknown"
    return f"nonsec:{slug}"


# --------------------------------------------------------------------------- Archetype & Authority Classification

def classify_document_archetype(
    text: str,
    *,
    title: str = "",
    retrieval_method: str = "user_supplied",
    rules: CompiledV3aRules | None = None,
) -> ArchetypeClassification:
    """Classify document archetype & 4-tier authority with explicit negative archetype filtering (R2-D1/R2-D6)."""
    compiled = rules or load_v3a_call_rules()
    head = (text or "")[:12_000]
    combined_head = f"{title}\n{head}"

    # 1. Explicit negative archetype filters (never auto-trigger on 10-K/10-Q/contracts/macro releases)
    for neg_kind, pats in compiled.negative_re.items():
        if any(p.search(head) for p in pats):
            auth: AuthorityTier = "sec_periodic_filing" if neg_kind == "form_10k_10q" else "unknown"
            return ArchetypeClassification(
                archetype=neg_kind,  # type: ignore[arg-type]
                authority_tier=auth,
                confidence=0.92,
                strong_feature_count=0,
                auto_trigger_eligible=False,
                negative_filter_hit=neg_kind,
                reasons=[f"negative_filter:{neg_kind}"],
            )

    # 2. Slide deck (.pptx or slide-marker rich IR PDF)
    if retrieval_method == "user_pptx":
        has_fin_cues = bool(re.search(r"\b(q[1-4]\s*fy?\d{2,4}|guidance|outlook|revenue|gross\s+margin|earnings|financial)\b|业绩|财报|指引|毛利率", combined_head, re.IGNORECASE))
        slide_marks = len(re.findall(r"\[Slide\s+\d+", text or ""))
        strong = (1 if slide_marks >= 2 else 0) + (1 if has_fin_cues else 0)
        return ArchetypeClassification(
            archetype="ir_slide_deck",
            authority_tier="management_first_party",
            confidence=0.90 if strong >= 2 else 0.72,
            strong_feature_count=strong,
            auto_trigger_eligible=strong >= 2,
            reasons=["pptx_slide_deck"] + (["financial_deck_cues"] if has_fin_cues else []),
        )

    # 3. Sell-side note cues vs Earnings transcript cues
    sellside_hits = sum(1 for p in compiled.sellside_pos_re if p.search(combined_head))
    transcript_hits = sum(1 for p in compiled.transcript_pos_re if p.search(combined_head))
    has_qa_header = any(p.search(text or "") for p in compiled.qa_header_re)
    has_operator = bool(re.search(r"(?m)^\s*(?:\[\d{2}:\d{2}(?::\d{2})?\]\s*)?(?:\*\*)?Operator(?:\*\*)?\s*[:\-–—]?\s*$", text or "", re.IGNORECASE)) or bool(
        re.search(r"\bnext\s+question\s+comes\s+from\b|\bour\s+first\s+question\s+comes\s+from\b", text or "", re.IGNORECASE)
    )
    speaker_colon_lines = len(re.findall(r"(?m)^\s*[A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3}(?:\s*\([^)]{2,40}\))?\s*:\s+\S", (text or "")[:40_000]))
    zh_speaker_lines = len(re.findall(r"(?m)^\s*(?:【(?:提问|回答)】|Q\d+[:：]|A\d+[:：]|[\u4e00-\u9fff]{2,8}(?:（[^）]{1,20}）)?[：:])", (text or "")[:40_000]))
    speaker_density = speaker_colon_lines + zh_speaker_lines

    # Strong transcript signal wins over incidental mention of analyst firm names (e.g., "Morgan Stanley" in Q&A!)
    transcript_strong = (
        (1 if transcript_hits >= 1 else 0)
        + (1 if (has_qa_header or has_operator) else 0)
        + (1 if speaker_density >= 4 else 0)
    )
    if transcript_strong >= 2 or (has_qa_header and speaker_density >= 2):
        conf = min(0.96, 0.65 + 0.10 * transcript_strong)
        return ArchetypeClassification(
            archetype="transcript",
            authority_tier="management_first_party",
            confidence=round(conf, 2),
            strong_feature_count=transcript_strong,
            auto_trigger_eligible=(conf >= 0.75 and transcript_strong >= 2),
            reasons=[
                r for r, flag in (
                    ("transcript_header_cues", transcript_hits >= 1),
                    ("qa_or_operator_structure", has_qa_header or has_operator),
                    ("speaker_turn_alternation", speaker_density >= 4),
                ) if flag
            ],
        )

    if sellside_hits >= 1 and speaker_density < 4:
        timing: Literal["pre_earnings_preview", "post_earnings_review", "unspecified"] = "unspecified"
        if any(p.search(combined_head) for p in compiled.sellside_preview_re):
            timing = "pre_earnings_preview"
        elif any(p.search(combined_head) for p in compiled.sellside_review_re):
            timing = "post_earnings_review"
        return ArchetypeClassification(
            archetype="sellside_note",
            authority_tier="third_party_opinion",
            confidence=0.84,
            strong_feature_count=sellside_hits,
            auto_trigger_eligible=False,  # sell-side notes alone never falsely trigger management audit
            sellside_timing=timing,
            reasons=["sellside_research_cues"],
        )

    # IR Slide Deck exported as PDF (multi-page short presentation slides with financial cues)
    page_breaks = (text or "").count("\f")
    if retrieval_method == "user_pdf" and page_breaks >= 4:
        avg_page_len = len(text or "") / max(1, page_breaks)
        has_fin_deck = bool(re.search(r"\b(investor\s+presentation|earnings\s+presentation|quarterly\s+results|guidance|outlook|non-?gaap\s+reconciliation)\b|业绩演示|业绩发布", combined_head, re.IGNORECASE))
        if avg_page_len < 1600 and has_fin_deck:
            return ArchetypeClassification(
                archetype="ir_slide_deck",
                authority_tier="management_first_party",
                confidence=0.80,
                strong_feature_count=2,
                auto_trigger_eligible=True,
                reasons=["pdf_slide_density", "financial_presentation_cues"],
            )

    if transcript_hits >= 1 or speaker_density >= 2:
        return ArchetypeClassification(
            archetype="transcript",
            authority_tier="management_first_party",
            confidence=0.68,
            strong_feature_count=1,
            auto_trigger_eligible=False,
            reasons=["weak_transcript_signal"],
        )

    return ArchetypeClassification(
        archetype="other",
        authority_tier="unknown",
        confidence=0.35,
        strong_feature_count=0,
        auto_trigger_eligible=False,
        reasons=["unclassified_document"],
    )


# --------------------------------------------------------------------------- Metadata & Fiscal Period Extraction

_FY_FQ_PATTERNS = (
    re.compile(r"\bQ([1-4])\s*(?:FY|Fiscal\s+Year\s*)?['’]?(\d{2,4})\b", re.IGNORECASE),
    re.compile(r"\b(?:FY|Fiscal\s+Year\s*)['’]?(\d{2,4})\s*Q([1-4])\b", re.IGNORECASE),
    re.compile(r"\b(First|Second|Third|Fourth)\s+Quarter\s+(?:Fiscal\s+)?(\d{4})\b", re.IGNORECASE),
    re.compile(r"(\d{4})\s*年?\s*第?([一二三四1-4])\s*季度", re.IGNORECASE),
)
_ORDINAL_Q = {"first": 1, "second": 2, "third": 3, "fourth": 4, "一": 1, "二": 2, "三": 3, "四": 4}
_MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_CALL_DATE_PATTERNS = (
    re.compile(r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(20\d{2})\b", re.IGNORECASE),
    re.compile(r"\b(20\d{2})[-/](\d{1,2})[-/](\d{1,2})\b"),
    re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
)
_TICKER_HINT_RE = re.compile(r"(?:\((?:NASDAQ|NYSE|AMEX|NYSE\s+ARCA)\s*:\s*([A-Z]{1,5}(?:\.[A-Z])?)\)|\b([A-Z]{2,5})\s+Q[1-4]\s*(?:FY)?\d{2,4}\b)")


def _norm_year(y_raw: str) -> int:
    y = int(y_raw)
    return 2000 + y if y < 100 else y


def extract_call_metadata(text: str, title: str = "") -> tuple[str | None, int | None, int | None, str | None]:
    """Extract ``(detected_symbol, fiscal_year, fiscal_quarter, call_date_iso)`` from title & header."""
    head = f"{title}\n{(text or '')[:4500]}"
    sym: str | None = None
    m_sym = _TICKER_HINT_RE.search(head)
    if m_sym:
        sym = (m_sym.group(1) or m_sym.group(2) or "").strip().upper() or None

    fy: int | None = None
    fq: int | None = None
    for idx, pat in enumerate(_FY_FQ_PATTERNS):
        m = pat.search(head)
        if not m:
            continue
        if idx == 0:
            fq, fy = int(m.group(1)), _norm_year(m.group(2))
        elif idx == 1:
            fy, fq = _norm_year(m.group(1)), int(m.group(2))
        elif idx == 2:
            fq = _ORDINAL_Q.get(m.group(1).lower())
            fy = int(m.group(2))
        else:
            fy = int(m.group(1))
            q_raw = m.group(2)
            fq = int(q_raw) if q_raw.isdigit() else _ORDINAL_Q.get(q_raw)
        if fy and fq:
            break

    call_date_iso: str | None = None
    for idx, pat in enumerate(_CALL_DATE_PATTERNS):
        m = pat.search(head)
        if not m:
            continue
        try:
            if idx == 0:
                mon = _MONTH_MAP.get(m.group(1).lower().rstrip("."))
                d = date(int(m.group(3)), mon or 1, int(m.group(2)))
            else:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if 2015 <= d.year <= 2035:
                call_date_iso = d.isoformat()
                break
        except ValueError:
            continue
    return sym, fy, fq, call_date_iso


# --------------------------------------------------------------------------- Speaker Directory & 6-Format State Machine

def _classify_speaker_role(name: str, title: str | None, firm: str | None, compiled: CompiledV3aRules) -> SpeakerRole:
    low_name = name.strip().casefold()
    if low_name in ("operator", "moderator", "主持人", "接线员"):
        return "operator"
    meta_str = f"{title or ''} {firm or ''}"
    if any(p.search(meta_str) for p in compiled.cfo_title_re):
        return "cfo"
    if any(p.search(meta_str) for p in compiled.ceo_title_re):
        return "ceo"
    if any(p.search(meta_str) for p in compiled.exec_title_re):
        return "executive"
    if firm and any(p.search(firm) for p in compiled.sellside_pos_re):
        return "analyst"
    if title and re.search(r"\b(?:analyst|research|managing\s+director|equity)\b|分析师|研究员", title, re.IGNORECASE):
        return "analyst"
    return "unknown"


def _extract_speaker_directory(text: str, compiled: CompiledV3aRules) -> dict[str, SpeakerMeta]:
    """Parse ``Company Participants`` / ``Conference Call Participants`` / ``Analysts`` blocks."""
    head = (text or "")[:12_000]
    directory: dict[str, SpeakerMeta] = {}
    section: Literal["none", "exec", "analyst"] = "none"

    for line in head.splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.casefold()
        if re.fullmatch(r"(?:company|corporate)\s+participants:?", low) or "管理层" in s and len(s) <= 20:
            section = "exec"
            continue
        if re.fullmatch(r"(?:conference\s+call\s+participants|analysts?):?", low) or "分析师名单" in s:
            section = "analyst"
            continue
        if re.fullmatch(r"(?:presentation|prepared\s+remarks|operator|transcript):?", low):
            break
        if section == "none":
            continue
        # Formats: "Colette Kress - Executive Vice President and CFO" or "Stacy Rasgon -- Bernstein"
        m = re.match(r"^([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff.\-' ]{1,48}?)\s*(?:[-–—]{1,2}|,|，)\s*(.{2,100})$", s)
        if not m:
            continue
        name = re.sub(r"\s+", " ", m.group(1)).strip()
        rest = m.group(2).strip()
        if section == "exec":
            role = _classify_speaker_role(name, rest, None, compiled)
            if role == "unknown":
                role = "executive"
            meta = SpeakerMeta(name=name, role=role, title=rest)
        else:
            meta = SpeakerMeta(name=name, role="analyst", firm=rest)
        directory[name.casefold()] = meta
        # Also index first name / last name shorthand if unique
        first_tok = name.split()[0].casefold()
        if len(first_tok) >= 3 and first_tok not in directory:
            directory[first_tok] = meta
    return directory


# Line-level speaker patterns (6 formats)
_FMT1_COLON_RE = re.compile(
    r"^\s*(?:\[(\d{2}:\d{2}(?::\d{2})?)\]\s*)?"
    r"(?:\*\*|#{1,4}\s*)?"
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3}|Operator|Moderator|[\u4e00-\u9fff]{2,8})"
    r"(?:\*\*)?"
    r"(?:\s*[（(]([^)）]{2,60})[)）])?"
    r"\s*[:：]\s*(.*)$"
)
_FMT4_MD_ROLE_RE = re.compile(
    r"^\s*(?:\*\*|#{1,4}\s*)([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){0,3}|Operator)(?:\*\*)?\s*[-–—]{1,2}\s*\*?([^*\n]{2,80})\*?\s*$"
)
_FMT5_ZH_QA_RE = re.compile(r"^\s*(?:【(提问|回答|追问)】|(Q\d+|A\d+)\s*[:：])\s*(.*)$")
_FMT2_STANDALONE_NAME_RE = re.compile(
    r"^\s*(?:\[(\d{2}:\d{2}(?::\d{2})?)\]\s*)?"
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){1,3}|Operator|Moderator)\s*$"
)
_OPERATOR_CALLOUT_RE = re.compile(
    r"(?:first|next|final|last)?\s*question(?:\s+today)?\s+(?:comes|is)\s+from\s+(?:the\s+line\s+of\s+)?"
    r"([A-Z][A-Za-z.\-']+(?:\s+[A-Z][A-Za-z.\-']+){1,3})(?:\s+(?:with|from|of|at)\s+([A-Za-z0-9&.,' \-]{2,50}))?",
    re.IGNORECASE,
)


def _extract_slide_refs(text: str, compiled: CompiledV3aRules) -> list[int]:
    hits: list[int] = []
    for pat in compiled.slide_ref_re:
        for m in pat.finditer(text or ""):
            for grp in m.groups():
                if grp and grp.isdigit():
                    num = int(grp)
                    if 1 <= num <= 300 and num not in hits:
                        hits.append(num)
    return hits


def _detect_call_default_basis(raw_text: str, compiled: CompiledV3aRules) -> CallDefaultBasis:
    """Inspect protected opening declaration (plus first 4,000 chars) for global Non-GAAP/GAAP basis."""
    opening = (raw_text or "")[: max(compiled.rules.context_budget.opening_basis_protect_chars, 4000)]
    if any(p.search(opening) for p in compiled.default_nongaap_re):
        return "non_gaap"
    if any(p.search(opening) for p in compiled.default_gaap_re):
        return "gaap"
    return "unspecified"


def _compute_boilerplate_spans(raw_text: str, compiled: CompiledV3aRules) -> list[tuple[int, int]]:
    """Compute non-destructive boilerplate spans while NEVER masking opening basis statements."""
    spans: list[tuple[int, int]] = []
    protect_end = compiled.rules.context_budget.opening_basis_protect_chars
    for pat in compiled.safe_harbor_re:
        for m in pat.finditer(raw_text or ""):
            s, e = m.start(), m.end()
            # Keep basis declarations in opening intact
            if s < protect_end and any(bp.search(raw_text[s:e]) for bp in (*compiled.default_nongaap_re, *compiled.default_gaap_re)):
                continue
            spans.append((s, e))
    # Page footer / copyright boilerplate lines
    for m in re.finditer(r"(?m)^[^\n]{0,120}(?:©\s*20\d{2}|All\s+Rights\s+Reserved|Page\s+\d+\s+of\s+\d+)[^\n]*$", raw_text or "", re.IGNORECASE):
        spans.append((m.start(), m.end()))
    spans.sort()
    return spans


def parse_transcript_v2(
    raw_text: str,
    *,
    source_id: str,
    title: str = "",
    content_sha256: str | None = None,
    retrieval_method: str = "user_supplied",
    document_pages: Mapping[str, Any] | None = None,
    owner_id: str | None = None,
    thesis_keywords: Sequence[str] = (),
    rules: CompiledV3aRules | None = None,
) -> ParsedCallDocument:
    """Parse earnings call transcript / deck / sell-side note preserving exact raw offsets."""
    compiled = rules or load_v3a_call_rules()
    text = raw_text or ""
    digest = content_sha256 or sha256(text.encode("utf-8")).hexdigest()

    subtitle_flag = is_subtitle_text(text)
    ts_anchors: list[tuple[int, str]] = []
    work_text = text
    if subtitle_flag:
        work_text, ts_anchors = clean_subtitle_text_with_timestamps(text)

    classification = classify_document_archetype(work_text, title=title, retrieval_method=retrieval_method, rules=compiled)
    det_sym, fy, fq, call_date_iso = extract_call_metadata(work_text, title=title)
    call_default_basis = _detect_call_default_basis(work_text, compiled)
    boilerplate_spans = _compute_boilerplate_spans(text, compiled) if not subtitle_flag else []

    open_gaps: list[str] = []
    if document_pages and isinstance(document_pages.get("visual_only_slides"), list):
        for s_num in document_pages["visual_only_slides"]:
            open_gaps.append(f"pptx_visual_only_slide:{s_num}")

    if classification.archetype == "ir_slide_deck":
        return ParsedCallDocument(
            owner_id=owner_id,
            source_id=source_id,
            content_sha256=digest,
            title=title,
            retrieval_method=retrieval_method,
            classification=classification,
            structure_mode="slide_deck",
            call_default_basis=call_default_basis,
            detected_symbol=det_sym,
            call_fiscal_year=fy,
            call_fiscal_quarter=fq,
            call_date_iso=call_date_iso,
            is_subtitle_source=False,
            boilerplate_spans=boilerplate_spans,
            open_gaps=open_gaps,
        )

    if classification.archetype == "sellside_note":
        return ParsedCallDocument(
            owner_id=owner_id,
            source_id=source_id,
            content_sha256=digest,
            title=title,
            retrieval_method=retrieval_method,
            classification=classification,
            structure_mode="sellside_note",
            call_default_basis=call_default_basis,
            detected_symbol=det_sym,
            call_fiscal_year=fy,
            call_fiscal_quarter=fq,
            call_date_iso=call_date_iso,
            is_subtitle_source=False,
            boilerplate_spans=boilerplate_spans,
            open_gaps=open_gaps,
        )

    speaker_dir = _extract_speaker_directory(work_text, compiled)

    # Locate Q&A boundary offset (if explicitly marked)
    qa_start_offset: int | None = None
    for pat in compiled.qa_header_re:
        m_qa = pat.search(work_text)
        if m_qa:
            qa_start_offset = m_qa.start()
            break

    # Line-by-line scan preserving exact raw character offsets
    line_spans: list[tuple[int, int, str]] = []
    pos = 0
    for raw_ln in work_text.splitlines(keepends=True):
        ln_clean = raw_ln.rstrip("\r\n")
        line_spans.append((pos, pos + len(ln_clean), ln_clean))
        pos += len(raw_ln)

    def _lookup_ts(char_off: int) -> str | None:
        if not ts_anchors:
            return None
        chosen: str | None = None
        for off, ts in ts_anchors:
            if off <= char_off:
                chosen = ts
            else:
                break
        return chosen

    turns: list[TranscriptTurn] = []
    cur_speaker: str | None = None
    cur_role: SpeakerRole = "unknown"
    cur_firm: str | None = None
    cur_start = 0
    cur_ts: str | None = None
    cur_lines: list[str] = []
    cur_end = 0
    pending_analyst_callout: tuple[str, str | None] | None = None
    in_qa = False

    def _flush_turn() -> None:
        nonlocal cur_speaker, cur_lines
        if cur_speaker is None:
            return
        body = "\n".join(cur_lines).strip()
        if not body:
            cur_speaker = None
            cur_lines = []
            return
        sec: Literal["prepared_remarks", "qa", "operator", "unstructured"]
        if cur_role == "operator":
            sec = "operator"
        elif in_qa or (qa_start_offset is not None and cur_start >= qa_start_offset):
            sec = "qa"
        else:
            sec = "prepared_remarks"

        # Detect operator callout naming the next analyst
        nonlocal pending_analyst_callout
        if cur_role == "operator":
            m_call = _OPERATOR_CALLOUT_RE.search(body)
            if m_call:
                pending_analyst_callout = (m_call.group(1).strip(), (m_call.group(2) or "").strip() or None)

        p_start = page_for_offset(dict(document_pages), cur_start) if (document_pages and not subtitle_flag) else None
        p_end = page_for_offset(dict(document_pages), max(cur_start, cur_end - 1)) if (document_pages and not subtitle_flag) else None
        turns.append(
            TranscriptTurn(
                turn_index=len(turns),
                section=sec,
                speaker_name=cur_speaker,
                speaker_role=cur_role,
                speaker_firm=cur_firm,
                text=body,
                raw_char_start=cur_start,
                raw_char_end=cur_end,
                page_start=p_start,
                page_end=p_end,
                timestamp_start=cur_ts or _lookup_ts(cur_start),
                slide_references=_extract_slide_refs(body, compiled),
            )
        )
        cur_speaker = None
        cur_lines = []

    def _resolve_speaker(name_raw: str, meta_hint: str | None) -> tuple[str, SpeakerRole, str | None]:
        nonlocal pending_analyst_callout
        name = re.sub(r"\s+", " ", name_raw).strip()
        dir_hit = speaker_dir.get(name.casefold())
        role = _classify_speaker_role(name, meta_hint, meta_hint, compiled)
        firm = meta_hint.strip() if (meta_hint and role in ("analyst", "unknown")) else None
        if dir_hit is not None:
            name = dir_hit.name
            if role == "unknown":
                role = dir_hit.role
            if not firm and dir_hit.firm:
                firm = dir_hit.firm
        if role == "unknown" and pending_analyst_callout is not None:
            pa_name, pa_firm = pending_analyst_callout
            if pa_name.casefold() in name.casefold() or name.casefold() in pa_name.casefold() or name.casefold() in ("analyst", "question", "提问"):
                name = pa_name
                role = "analyst"
                firm = firm or pa_firm
                pending_analyst_callout = None
        return name, role, firm

    # Detect end of initial Participants Directory header block so roster lines are not treated as turns
    dir_end_line = 0
    in_dir_block = False
    _DIR_START_RE = re.compile(r"^(?:company|corporate|conference\s+call|executive)\s+participants:?$|^analysts?:?$", re.IGNORECASE)
    _DIR_STOP_RE = re.compile(r"^(?:presentation|prepared\s+remarks|transcript|opening\s+remarks|management\s+discussion):?$", re.IGNORECASE)
    for idx_l, (_ls, _le, ltxt) in enumerate(line_spans[:80]):
        st_l = ltxt.strip()
        if not st_l:
            continue
        if _DIR_START_RE.match(st_l):
            in_dir_block = True
            dir_end_line = idx_l + 1
            continue
        if in_dir_block:
            if _DIR_STOP_RE.match(st_l):
                dir_end_line = idx_l + 1
                break
            if ":" in st_l or "：" in st_l:
                break
            dir_end_line = idx_l + 1

    _NON_SPEAKER_HEADINGS = frozenset({
        "company participants",
        "corporate participants",
        "conference call participants",
        "executive participants",
        "call participants",
        "prepared remarks",
        "opening remarks",
        "presentation",
        "analysts",
        "participants",
        "questions and answers",
        "question and answer",
    })

    i = dir_end_line
    n_lines = len(line_spans)
    while i < n_lines:
        l_start, l_end, ln = line_spans[i]
        stripped = ln.strip()
        if not stripped:
            if cur_speaker is not None:
                cur_lines.append("")
            i += 1
            continue

        if any(p.match(stripped) for p in compiled.qa_header_re):
            _flush_turn()
            in_qa = True
            i += 1
            continue

        # Format 5: Chinese 【提问】 / 【回答】 / Q1: / A1:
        m5 = _FMT5_ZH_QA_RE.match(stripped)
        if m5:
            _flush_turn()
            in_qa = True
            tag = m5.group(1) or m5.group(2) or ""
            rest = m5.group(3) or ""
            is_q = tag in ("提问", "追问") or tag.upper().startswith("Q")
            cur_speaker = (pending_analyst_callout[0] if (is_q and pending_analyst_callout) else ("Analyst" if is_q else "Management"))
            cur_role = "analyst" if is_q else "executive"
            cur_firm = pending_analyst_callout[1] if (is_q and pending_analyst_callout) else None
            if is_q:
                pending_analyst_callout = None
            cur_start = l_start
            cur_end = l_end
            cur_ts = _lookup_ts(l_start)
            cur_lines = [rest] if rest else []
            i += 1
            continue

        # Format 4: Markdown **Name** -- *Role/Firm*
        m4 = _FMT4_MD_ROLE_RE.match(stripped)
        if m4:
            _flush_turn()
            cur_speaker, cur_role, cur_firm = _resolve_speaker(m4.group(1), m4.group(2))
            if cur_role == "analyst":
                in_qa = True
            cur_start = l_start
            cur_end = l_end
            cur_ts = _lookup_ts(l_start)
            cur_lines = []
            i += 1
            continue

        # Format 1 & 3: [00:14:22] Name (Firm): text / 黄仁勋：text
        m1 = _FMT1_COLON_RE.match(stripped)
        if m1 and len(m1.group(2).strip()) <= 44:
            cand_name = m1.group(2).strip()
            # Guard against false positives like "Note: ..." or "Summary: ..."
            if cand_name.casefold() not in {
                "note", "notes", "summary", "highlights", "guidance", "outlook", "revenue",
                "margin", "ebitda", "gaap", "non-gaap", "disclaimer", "source", "date", "time",
                "total", "subtotal", "fiscal", "quarter", "slide", "chart", "table", "remarks",
            }:
                _flush_turn()
                cur_ts = m1.group(1) or _lookup_ts(l_start)
                cur_speaker, cur_role, cur_firm = _resolve_speaker(cand_name, m1.group(3))
                if cur_role == "analyst":
                    in_qa = True
                cur_start = l_start
                cur_end = l_end
                body_tail = (m1.group(4) or "").strip()
                cur_lines = [body_tail] if body_tail else []
                i += 1
                continue

        # Format 2: Standalone Speaker Name line followed by Role/Firm line (Motley Fool / Seeking Alpha / Quartr)
        m2 = _FMT2_STANDALONE_NAME_RE.match(stripped)
        if m2 and i + 1 < n_lines:
            cand_name = m2.group(2).strip()
            if cand_name.casefold() in _NON_SPEAKER_HEADINGS:
                i += 1
                continue
            next_ln = line_spans[i + 1][2].strip()
            is_known = cand_name.casefold() in speaker_dir or cand_name.casefold() in ("operator", "moderator")
            next_looks_role = bool(next_ln) and len(next_ln) <= 85 and (
                _classify_speaker_role(cand_name, next_ln, next_ln, compiled) != "unknown"
                or bool(re.search(r"--|\b(?:analyst|ceo|cfo|president|vp|svp|evp|investor\s+relations|research|capital|securities|partners|bank|group)\b", next_ln, re.IGNORECASE))
            )
            if is_known or next_looks_role:
                _flush_turn()
                cur_ts = m2.group(1) or _lookup_ts(l_start)
                meta_hint = next_ln if next_looks_role else None
                cur_speaker, cur_role, cur_firm = _resolve_speaker(cand_name, meta_hint)
                if cur_role == "analyst":
                    in_qa = True
                cur_start = l_start
                cur_end = line_spans[i + 1][1] if next_looks_role else l_end
                cur_lines = []
                i += 2 if next_looks_role else 1
                continue

        # Continuation line of active turn
        if cur_speaker is not None:
            cur_lines.append(stripped)
            cur_end = l_end
        i += 1

    _flush_turn()

    # Honest fallback when no speaker structure was found
    if len(turns) < 2:
        open_gaps.append("unstructured_transcript_fallback:no_speaker_turns_detected")
        return ParsedCallDocument(
            owner_id=owner_id,
            source_id=source_id,
            content_sha256=digest,
            title=title,
            retrieval_method=retrieval_method,
            classification=classification,
            structure_mode="unstructured_fallback",
            call_default_basis=call_default_basis,
            detected_symbol=det_sym,
            call_fiscal_year=fy,
            call_fiscal_quarter=fq,
            call_date_iso=call_date_iso,
            is_subtitle_source=subtitle_flag,
            boilerplate_spans=boilerplate_spans,
            speaker_directory=speaker_dir,
            turns=turns,
            qa_exchanges=[],
            open_gaps=open_gaps,
        )

    # Infer roles for unknown speakers inside Q&A (alternating analyst question -> executive answer)
    for idx, t in enumerate(turns):
        if t.section == "qa" and t.speaker_role == "unknown":
            if "?" in t.text or "？" in t.text:
                turns[idx] = t.model_copy(update={"speaker_role": "analyst"})
            elif idx > 0 and turns[idx - 1].speaker_role == "analyst":
                turns[idx] = t.model_copy(update={"speaker_role": "executive"})

    # Build slide cross-anchor map
    slide_ref_map: dict[int, list[int]] = {}
    for t in turns:
        for s_num in t.slide_references:
            slide_ref_map.setdefault(s_num, []).append(t.turn_index)

    # Pair Q&A Exchanges (analyst question + executive response + same-analyst follow-ups)
    qa_exchanges = _pair_and_score_qa_exchanges(turns, thesis_keywords=thesis_keywords, compiled=compiled)

    # Compute per-executive baseline stats (for multi-quarter Speaker-Baseline Shift)
    exec_baselines = _compute_executive_baselines(qa_exchanges)

    return ParsedCallDocument(
        owner_id=owner_id,
        source_id=source_id,
        content_sha256=digest,
        title=title,
        retrieval_method=retrieval_method,
        classification=classification,
        structure_mode="structured_transcript",
        call_default_basis=call_default_basis,
        detected_symbol=det_sym,
        call_fiscal_year=fy,
        call_fiscal_quarter=fq,
        call_date_iso=call_date_iso,
        is_subtitle_source=subtitle_flag,
        boilerplate_spans=boilerplate_spans,
        speaker_directory=speaker_dir,
        turns=turns,
        qa_exchanges=qa_exchanges,
        slide_references_map=slide_ref_map,
        executive_baselines=exec_baselines,
        open_gaps=open_gaps,
    )


_NUMERIC_SIGNAL_RE = re.compile(r"(?:\$\s*\d|\d+(?:\.\d+)?\s*(?:%|bps|billion|million|thousand|x\b|B\b|M\b)|亿|万|个百分点)")
_MULTIPART_Q_RE = re.compile(r"\b(?:first(?:ly)?|second(?:ly)?|two\s+(?:questions|parts)|also\s+wanted\s+to\s+ask|my\s+second\s+question|follow-?up)\b|第一|第二|另外想问", re.IGNORECASE)


def _pair_and_score_qa_exchanges(
    turns: Sequence[TranscriptTurn],
    *,
    thesis_keywords: Sequence[str],
    compiled: CompiledV3aRules,
) -> list[QaExchange]:
    qa_turns = [t for t in turns if t.section == "qa" and t.speaker_role != "operator"]
    if not qa_turns:
        return []

    raw_groups: list[tuple[list[TranscriptTurn], list[TranscriptTurn]]] = []
    cur_q: list[TranscriptTurn] = []
    cur_a: list[TranscriptTurn] = []
    cur_analyst: str | None = None

    for t in qa_turns:
        if t.speaker_role == "analyst":
            same_analyst = cur_analyst is not None and t.speaker_name.casefold() == cur_analyst.casefold()
            if cur_q and cur_a and not same_analyst:
                raw_groups.append((cur_q, cur_a))
                cur_q = [t]
                cur_a = []
                cur_analyst = t.speaker_name
            else:
                cur_q.append(t)
                cur_analyst = t.speaker_name
        else:
            if cur_q:
                cur_a.append(t)

    if cur_q and cur_a:
        raw_groups.append((cur_q, cur_a))

    # First pass: topic keywords per exchange for multi-analyst clustering
    topic_counts: dict[str, int] = {}
    per_ex_tokens: list[list[str]] = []
    _stop = {"quarter", "year", "question", "thanks", "thank", "great", "good", "morning", "afternoon", "guys", "team", "little", "bit", "about", "think", "going", "would", "could", "really", "actually"}
    for q_list, _a_list in raw_groups:
        q_concat = " ".join(t.text for t in q_list)
        toks = [tok for tok in tokenize_query(q_concat) if len(tok) >= 4 and tok not in _stop]
        uniq = list(dict.fromkeys(toks))[:12]
        per_ex_tokens.append(uniq)
        for tok in set(uniq):
            topic_counts[tok] = topic_counts.get(tok, 0) + 1

    thesis_toks = {tok for kw in thesis_keywords for tok in tokenize_query(kw) if len(tok) >= 3}
    weights = compiled.rules.evasion_scoring.prior_weights

    exchanges: list[QaExchange] = []
    for idx, ((q_list, a_list), q_toks) in enumerate(zip(raw_groups, per_ex_tokens)):
        q_text = "\n".join(t.text for t in q_list)
        a_text = "\n".join(f"{t.speaker_name}: {t.text}" if len(a_list) > 1 else t.text for t in a_list)
        a_plain = " ".join(t.text for t in a_list)
        a_low = a_plain.casefold()

        asked_quant = any(p.search(q_text) for p in compiled.quant_q_re)
        num_hits_a = len(_NUMERIC_SIGNAL_RE.findall(a_plain))
        answered_quant = num_hits_a >= 1

        # Entity overlap in first 40% of answer
        first_40 = a_low[: max(120, int(len(a_low) * 0.40))]
        a_toks_40 = set(tokenize_query(first_40))
        overlap = (sum(1 for tok in q_toks if tok in a_toks_40) / float(len(q_toks))) if q_toks else 1.0

        # Hedge density per 100 words
        word_count = max(1, len(a_plain.split()))
        hedge_hits = sum(a_low.count(h) for h in compiled.hedge_terms)
        hedge_density = min(1.0, hedge_hits / float(word_count))

        verbosity_ratio = round(len(a_plain) / float(max(len(q_text), 80)), 2)
        numeric_guidance_density = round(min(1.0, num_hits_a / max(1.0, word_count / 40.0)), 3)
        multi_part = (q_text.count("?") + q_text.count("？") >= 2) or bool(_MULTIPART_Q_RE.search(q_text))
        cluster_hit = any(topic_counts.get(tok, 0) >= 2 for tok in q_toks)
        thesis_hit = bool(thesis_toks and any(tok in thesis_toks for tok in q_toks))

        f_quant_unans = 1.0 if (asked_quant and not answered_quant) else 0.0
        f_low_overlap = max(0.0, 1.0 - overlap)
        f_hedge_verb = min(1.0, hedge_density * 15.0 + max(0.0, (verbosity_ratio - 2.5) * 0.15))
        f_cluster = 1.0 if cluster_hit else 0.0
        f_thesis = 1.0 if thesis_hit else 0.0

        prior_score = round(
            weights.get("asked_quant_unanswered", 0.30) * f_quant_unans
            + weights.get("low_entity_overlap", 0.25) * f_low_overlap
            + weights.get("hedge_and_verbosity", 0.20) * f_hedge_verb
            + weights.get("multi_analyst_cluster", 0.15) * f_cluster
            + weights.get("thesis_or_flag_hit", 0.10) * f_thesis,
            4,
        )

        exec_names = list(dict.fromkeys(t.speaker_name for t in a_list))
        exec_roles = list(dict.fromkeys(t.speaker_role for t in a_list))
        slide_refs = sorted({s for t in (*q_list, *a_list) for s in t.slide_references})

        exchanges.append(
            QaExchange(
                exchange_index=idx + 1,
                analyst_name=q_list[0].speaker_name,
                analyst_firm=q_list[0].speaker_firm,
                executive_names=exec_names,
                executive_roles=exec_roles,
                question_text=q_text,
                answer_text=a_text,
                followup_count=max(0, len(q_list) - 1),
                raw_char_start=q_list[0].raw_char_start,
                raw_char_end=a_list[-1].raw_char_end,
                question_raw_start=q_list[0].raw_char_start,
                question_raw_end=q_list[-1].raw_char_end,
                answer_raw_start=a_list[0].raw_char_start,
                answer_raw_end=a_list[-1].raw_char_end,
                page_start=q_list[0].page_start,
                page_end=a_list[-1].page_end,
                timestamp_start=q_list[0].timestamp_start,
                slide_references=slide_refs,
                asked_quantitative=asked_quant,
                answered_quantitative=answered_quant,
                entity_overlap_ratio=round(overlap, 3),
                hedge_density=round(hedge_density, 4),
                verbosity_ratio=verbosity_ratio,
                numeric_guidance_density=numeric_guidance_density,
                multi_part_question_hit=multi_part,
                analyst_cluster_topic_hit=cluster_hit,
                thesis_or_flag_hit=thesis_hit,
                evasion_prior_score=prior_score,
                topic_keywords=q_toks[:8],
            )
        )
    return exchanges


def _compute_executive_baselines(exchanges: Sequence[QaExchange]) -> list[ExecutiveBaselineStats]:
    buckets: dict[str, dict[str, Any]] = {}
    for ex in exchanges:
        for idx, name in enumerate(ex.executive_names):
            role = ex.executive_roles[idx] if idx < len(ex.executive_roles) else "executive"
            key = name.casefold()
            b = buckets.setdefault(key, {"name": name, "role": role, "n": 0, "hedge": 0.0, "verb": 0.0, "ov": 0.0})
            b["n"] += 1
            b["hedge"] += ex.hedge_density
            b["verb"] += ex.verbosity_ratio
            b["ov"] += ex.entity_overlap_ratio
    out: list[ExecutiveBaselineStats] = []
    for b in buckets.values():
        n = max(1, b["n"])
        out.append(
            ExecutiveBaselineStats(
                executive_name=b["name"],
                speaker_role=b["role"],
                answer_count=b["n"],
                avg_hedge_density=round(b["hedge"] / n, 4),
                avg_verbosity_ratio=round(b["verb"] / n, 2),
                avg_entity_overlap=round(b["ov"] / n, 3),
            )
        )
    return out


def build_direction_a_transcript_index_from_user_source(
    source: Any,
    *,
    symbol: str,
    rules: CompiledV3aRules | None = None,
) -> tuple[Any | None, ParsedCallDocument | None]:
    """Build a Direction A ``TranscriptIndexEntry`` from a Topic-attached user transcript (D6).

    Strictly preserves the user source's real ``source_id``, ``retrieval_method``, ``kind``,
    and ``permissions`` (never forging ``fmp_transcript`` or ``export=False``).
    """
    from .research_earnings_squad import TranscriptIndexEntry

    text = getattr(source, "text", "") or ""
    if len(text) < 400:
        return None, None
    doc_pages = (getattr(source, "provenance", None) or {}).get("document_pages")
    parsed = parse_transcript_v2(
        text,
        source_id=source.source_id,
        title=source.title,
        content_sha256=source.content_sha256,
        retrieval_method=source.retrieval_method,
        document_pages=doc_pages if isinstance(doc_pages, Mapping) else None,
        owner_id=getattr(source, "owner_id", None),
        rules=rules,
    )
    if parsed.classification.archetype != "transcript" or parsed.structure_mode == "unstructured_fallback":
        return None, parsed

    sections = list(dict.fromkeys(t.section for t in parsed.turns if t.section in ("prepared_remarks", "qa", "operator")))
    speakers = list(dict.fromkeys(t.speaker_name for t in parsed.turns if t.speaker_name))[:40]
    perms_obj = getattr(source, "permissions", None)
    perms_dict = (
        perms_obj.model_dump(mode="json")
        if hasattr(perms_obj, "model_dump")
        else {"store": True, "model_input": True, "display": True, "export": True}
    )
    call_dt: date | None = None
    if parsed.call_date_iso:
        try:
            call_dt = date.fromisoformat(parsed.call_date_iso)
        except ValueError:
            call_dt = None
    entry = TranscriptIndexEntry(
        status="available",
        provider="user_uploaded_v3a",
        symbol=symbol.upper(),
        fiscal_year=parsed.call_fiscal_year,
        fiscal_quarter=parsed.call_fiscal_quarter,
        call_date=call_dt,
        sections=sections,
        speakers=speakers,
        source_id=source.source_id,
        content_sha256=source.content_sha256,
        permissions=perms_dict,
    )
    return entry, parsed
