"""Versioned research method packs (W10).

A pack describes a research *method* (evidence requirements, counter-evidence,
unit/period checks, reconciliation, stopping conditions, report structure). It
never contains sample-specific facts. Packs are selected deterministically from
the question and rendered into the run's first user message (never SYSTEM/TOOLS,
so the cacheable prefix is unchanged).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PACKS_DIR = Path(__file__).parent / 'packs'
MAX_PACK_CHARS = 3500
MAX_INDUSTRY_PACK_CHARS = 4500
MAX_COMPOSED_PACK_CHARS = 8000
MIN_SELECT_SCORE = 2

# Tools a pack may name. search_library / news_search / earnings_consensus /
# earnings_transcript are optional and may be absent in a given run; the renderer
# marks anything unavailable.
KNOWN_TOOLS = frozenset({
    'resolve_entity', 'market_data', 'company_financials', 'filings',
    'acquire_sources', 'read_sources', 'read_pages', 'outline_source', 'search_source',
    'request_ocr', 'record_facts', 'calculate_financials', 'create_chart', 'submit_research',
    'search_library', 'news_search', 'issuer_documents', 'extract_statements',
    'sec_earnings_release', 'sec_filing_tables', 'sec_filing_sections',
    'sec_redline_diff', 'sec_fulltext_search',
    'earnings_consensus', 'earnings_transcript',
    'earnings_quality_diagnostics', 'guidance_and_nongaap_reconciler',
    'valuation_and_implied_expectations',
    'bank_fundamentals_diagnostics', 'bank_valuation_and_peer_compare',
})
OPTIONAL_TOOLS = frozenset({
    'search_library',
    'news_search',
    'earnings_consensus',
    'earnings_transcript',
})
# tool -> capability flag in the run's available_capabilities dict
CAPABILITY_FLAG = {
    'market_data': 'market_data_available',
    'company_financials': 'sec_financials_available',
    'filings': 'filings_available',
    'acquire_sources': 'acquire_available',
    'search_library': 'search_library_available',
    'news_search': 'news_search_available',
    'issuer_documents': 'issuer_documents_available',
    'sec_earnings_release': 'filings_available',
    'sec_filing_tables': 'filings_available',
    'sec_fulltext_search': 'filings_available',
    'earnings_consensus': 'earnings_consensus_available',
    'earnings_transcript': 'earnings_transcript_available',
    'bank_fundamentals_diagnostics': 'sec_financials_available',
    'bank_valuation_and_peer_compare': 'sec_financials_available',
}

INDUSTRY_PACK_IDS: tuple[str, ...] = (
    'kpi_saas_cloud',
    'kpi_semis_hardware',
    'kpi_internet_ads',
    'kpi_consumer_retail',
    'kpi_pharma_biotech',
    'kpi_us_large_banks',
)

# Disjoint SEC SIC ranges mapped to the 5 non-financial L2 industry KPI packs.
# Financial SIC codes (6000-6799, including Banks, Insurance, and REITs) never match.
SIC_INDUSTRY_RANGES: tuple[tuple[int, int, str], ...] = (
    # Software & Cloud
    (7371, 7374, 'kpi_saas_cloud'),
    (7376, 7379, 'kpi_saas_cloud'),
    # Semiconductors, Hardware & AI Infra
    (3559, 3559, 'kpi_semis_hardware'),
    (3570, 3579, 'kpi_semis_hardware'),
    (3661, 3669, 'kpi_semis_hardware'),
    (3670, 3679, 'kpi_semis_hardware'),
    (3690, 3699, 'kpi_semis_hardware'),
    (3825, 3829, 'kpi_semis_hardware'),
    # Internet, Platforms & Digital Ads
    (4832, 4833, 'kpi_internet_ads'),
    (4841, 4841, 'kpi_internet_ads'),
    (4899, 4899, 'kpi_internet_ads'),
    (7310, 7319, 'kpi_internet_ads'),
    (7370, 7370, 'kpi_internet_ads'),
    (7375, 7375, 'kpi_internet_ads'),
    (7812, 7812, 'kpi_internet_ads'),
    (7841, 7841, 'kpi_internet_ads'),
    # Consumer & Retail
    (2000, 2199, 'kpi_consumer_retail'),
    (2300, 2399, 'kpi_consumer_retail'),
    (2840, 2844, 'kpi_consumer_retail'),
    (3021, 3021, 'kpi_consumer_retail'),
    (3140, 3149, 'kpi_consumer_retail'),
    (5200, 5999, 'kpi_consumer_retail'),
    # Pharma & Biotech
    (2830, 2836, 'kpi_pharma_biotech'),
    (3841, 3845, 'kpi_pharma_biotech'),
    (3851, 3851, 'kpi_pharma_biotech'),
    (8071, 8071, 'kpi_pharma_biotech'),
    (8731, 8731, 'kpi_pharma_biotech'),
)


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


def _validate_known_tools(tools: list[str]) -> list[str]:
    unknown = [t for t in tools if t not in KNOWN_TOOLS]
    if unknown:
        raise ValueError(f'unknown tools: {unknown}')
    if len(set(tools)) != len(tools):
        raise ValueError('duplicate tools')
    return tools


def _dedup_preserve(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class Trigger(_Model):
    term: str = Field(min_length=1)
    weight: int = Field(ge=1, le=3)


class AppliesWhen(_Model):
    question_types: list[str] = Field(min_length=1)
    triggers: list[Trigger] = Field(min_length=1)


class EvidenceRequirement(_Model):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    description: str = Field(min_length=1)
    preferred_tools: list[str] = Field(default_factory=list)  # empty = reasoning step, no tool
    required: bool
    freshness: str = Field(min_length=1)
    acceptance: str = Field(min_length=1)
    fallback: str = Field(min_length=1)

    @field_validator('preferred_tools')
    @classmethod
    def _known_tools(cls, tools: list[str]) -> list[str]:
        return _validate_known_tools(tools)


class CounterEvidence(_Model):
    look_for: list[str] = Field(min_length=1)
    min_search_actions: int = Field(ge=1)
    if_none_found: str = Field(min_length=1)


class ReconciliationItem(_Model):
    metric: str = Field(min_length=1)
    source_classes: list[str] = Field(min_length=2)
    note: str = Field(min_length=1)


class ReportStructure(_Model):
    sections: list[str] = Field(min_length=1)
    must_state: list[str] = Field(min_length=1)

    @model_validator(mode='after')
    def _dates_and_gaps(self) -> 'ReportStructure':
        text = ' '.join(self.must_state).lower()
        if 'as-of' not in text or 'coverage gap' not in text:
            raise ValueError('must_state must require as-of dates and coverage gaps')
        return self


class Pack(_Model):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    title_zh: str = Field(min_length=1)
    title_en: str = Field(min_length=1)
    applies_when: AppliesWhen
    evidence_requirements: list[EvidenceRequirement] = Field(min_length=1)
    counter_evidence: CounterEvidence
    unit_and_period_checks: list[str] = Field(min_length=1)
    reconciliation: list[ReconciliationItem] = Field(default_factory=list)
    stopping_conditions: list[str] = Field(min_length=1)
    report_structure: ReportStructure
    limits: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def _checks(self) -> 'Pack':
        ids = [r.id for r in self.evidence_requirements]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate evidence requirement ids')
        if not any(r.required for r in self.evidence_requirements):
            raise ValueError('at least one evidence requirement must be required')
        return self


class IndustryKpiSpec(_Model):
    id: str = Field(pattern=r'^[a-z][a-z0-9_]*$')
    name_en: str = Field(min_length=1)
    name_zh: str = Field(min_length=1)
    definition_and_formula: str = Field(min_length=1)
    preferred_tools: list[str] = Field(min_length=1)
    where_to_look: str = Field(min_length=1)
    red_flags: list[str] = Field(min_length=1)
    required: bool = True

    @field_validator('preferred_tools')
    @classmethod
    def _known_tools(cls, tools: list[str]) -> list[str]:
        return _validate_known_tools(tools)


class BearCaseAngle(_Model):
    angle: str = Field(min_length=1)
    evidence_anchor: str = Field(min_length=1)
    next_quarter_trigger: str = Field(min_length=1)


class IndustryKpiPack(_Model):
    """L2 non-financial industry KPI plugin layered onto the L1 earnings backbone."""

    id: str = Field(pattern=r'^kpi_[a-z0-9_]+$')
    version: str = Field(pattern=r'^\d+\.\d+\.\d+$')
    layer: Literal['l2_industry_kpi'] = 'l2_industry_kpi'
    parent_pack_id: Literal['earnings_review'] = 'earnings_review'
    title_zh: str = Field(min_length=1)
    title_en: str = Field(min_length=1)
    sector_scope: str = Field(min_length=1)
    sic_major_groups: list[str] = Field(min_length=1)
    kpis: list[IndustryKpiSpec] = Field(min_length=1)
    filing_focus: list[str] = Field(min_length=1)
    counter_evidence_angles: list[BearCaseAngle] = Field(min_length=1)
    unit_and_period_checks: list[str] = Field(min_length=1)
    reconciliation: list[ReconciliationItem] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)

    @field_validator('sic_major_groups')
    @classmethod
    def _valid_major_groups(cls, groups: list[str]) -> list[str]:
        for grp in groups:
            if not re.fullmatch(r'\d{2}', grp):
                raise ValueError(f'invalid 2-digit SIC major group: {grp!r}')
        if len(set(groups)) != len(groups):
            raise ValueError('duplicate sic_major_groups')
        return groups

    @model_validator(mode='after')
    def _checks(self) -> 'IndustryKpiPack':
        ids = [k.id for k in self.kpis]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate industry KPI ids')
        if not any(k.required for k in self.kpis):
            raise ValueError('at least one industry KPI must be required')
        return self


class ComposedEarningsMethodPack(_Model):
    """L1 US earnings backbone combined with an optional L2 industry KPI pack."""

    base_pack: Pack
    industry_pack: IndustryKpiPack | None = None
    sic_code: str | None = None
    matched_industry_pack_id: str | None = None
    industry_pack_id: str | None = None
    sector_category: Literal['non_financial', 'bank', 'insurance', 'reit', 'financial_other'] = 'non_financial'
    is_sector_gated: bool = False
    active_sector_plugin: str | None = None
    selection_source: Literal['none', 'sic_auto', 'explicit_override', 'sector_gated', 'sector_plugin']
    fingerprint: str
    pack_records: list[dict[str, Any]]
    required_blocks: list[str]
    unit_and_period_checks: list[str]
    reconciliation: list[ReconciliationItem]
    counter_evidence_look_for: list[str]
    limits: list[str]
    rendered_prompt: str


class SelectionResult(_Model):
    pack_id: str
    score: int
    matched_terms: list[str]


def pack_fingerprint(pack: Pack | IndustryKpiPack) -> str:
    """Stable id for run records: ``<id>@<version>+<sha256[:12] of canonical content>``."""
    canonical = json.dumps(pack.model_dump(mode='json'), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]
    return f'{pack.id}@{pack.version}+{digest}'


def industry_pack_fingerprint(pack: IndustryKpiPack) -> str:
    """Stable id for an L2 industry KPI pack."""
    return pack_fingerprint(pack)


@lru_cache(maxsize=1)
def _load() -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    for path in sorted(PACKS_DIR.glob('*.json')):
        if path.stem.startswith('kpi_'):
            continue
        pack = Pack.model_validate_json(path.read_text(encoding='utf-8'))
        if pack.id != path.stem:
            raise ValueError(f'pack id {pack.id!r} does not match file {path.name}')
        packs[pack.id] = pack
    return packs


@lru_cache(maxsize=1)
def _load_industry_packs() -> dict[str, IndustryKpiPack]:
    packs: dict[str, IndustryKpiPack] = {}
    for path in sorted(PACKS_DIR.glob('kpi_*.json')):
        pack = IndustryKpiPack.model_validate_json(path.read_text(encoding='utf-8'))
        if pack.id != path.stem:
            raise ValueError(f'industry pack id {pack.id!r} does not match file {path.name}')
        packs[pack.id] = pack
    return packs


def load_packs() -> dict[str, Pack]:
    return dict(_load())


def load_industry_packs() -> dict[str, IndustryKpiPack]:
    """Return all validated L2 non-financial industry KPI packs keyed by pack id."""
    return dict(_load_industry_packs())


def get_pack(pack_id: str) -> Pack | None:
    """Return the validated ``Pack`` for ``pack_id``, or ``None`` if unknown."""
    if not isinstance(pack_id, str):
        return None
    return _load().get(pack_id.strip())


def get_industry_pack(pack_id: str) -> IndustryKpiPack | None:
    """Return the validated L2 ``IndustryKpiPack`` for ``pack_id``, or ``None`` if unknown."""
    if not isinstance(pack_id, str):
        return None
    return _load_industry_packs().get(pack_id.strip())


def list_available_packs() -> list[dict[str, Any]]:
    """Return sorted metadata summaries for all available research method packs."""
    return [
        {
            'pack_id': pack.id,
            'version': pack.version,
            'fingerprint': pack_fingerprint(pack),
            'title_en': pack.title_en,
            'title_zh': pack.title_zh,
            'question_types': list(pack.applies_when.question_types),
        }
        for _, pack in sorted(_load().items())
    ]


def list_industry_packs() -> list[dict[str, Any]]:
    """Return sorted metadata summaries for all available L2 industry KPI packs."""
    return [
        {
            'pack_id': pack.id,
            'version': pack.version,
            'layer': pack.layer,
            'parent_pack_id': pack.parent_pack_id,
            'fingerprint': pack_fingerprint(pack),
            'title_en': pack.title_en,
            'title_zh': pack.title_zh,
            'sector_scope': pack.sector_scope,
            'sic_major_groups': list(pack.sic_major_groups),
            'kpi_ids': [k.id for k in pack.kpis],
        }
        for _, pack in sorted(_load_industry_packs().items())
    ]


def resolve_active_packs(pack_records: Sequence[Mapping[str, Any]] | None) -> list[Pack]:
    """Resolve active ``Pack`` models deterministically from ``state['method_packs']`` records."""
    if not pack_records:
        return []
    packs = _load()
    resolved: list[Pack] = []
    seen: set[str] = set()
    for record in pack_records:
        if not isinstance(record, Mapping):
            continue
        pack_id = record.get('pack_id')
        if isinstance(pack_id, str) and pack_id in packs and pack_id not in seen:
            seen.add(pack_id)
            resolved.append(packs[pack_id])
    return resolved


def tool_available_in_run(
    tool: str,
    caps: Mapping[str, Any] | None = None,
    *,
    has_fin_data: bool | None = None,
    has_eligible_sources: bool = True,
) -> bool:
    """Return whether ``tool`` can actually execute in the current run configuration."""
    c = caps or {}
    fin_flags = (
        'market_data_available',
        'sec_financials_available',
        'filings_available',
        'cn_market_data_available',
        'cn_financials_available',
        'hk_market_data_available',
    )
    if tool == 'resolve_entity':
        if has_fin_data is not None:
            return has_fin_data
        return any(bool(c.get(flag)) for flag in fin_flags)
    if tool == 'market_data':
        return bool(c.get('market_data_available') or c.get('cn_market_data_available') or c.get('hk_market_data_available'))
    if tool == 'company_financials':
        return bool(c.get('sec_financials_available') or c.get('cn_financials_available'))
    if tool in ('filings', 'sec_earnings_release', 'sec_filing_tables', 'sec_fulltext_search'):
        return bool(c.get('filings_available'))
    if tool == 'earnings_consensus':
        return bool(c.get('earnings_consensus_available'))
    if tool == 'earnings_transcript':
        return bool(c.get('earnings_transcript_available'))
    if tool == 'issuer_documents':
        return bool(c.get('issuer_documents_available'))
    if tool == 'news_search':
        return bool(c.get('news_search_available'))
    if tool == 'acquire_sources':
        return bool(c.get('acquire_available'))
    can_acquire_sources = (
        bool(c.get('acquire_available'))
        or bool(c.get('sec_financials_available') or c.get('cn_financials_available'))
        or bool(c.get('filings_available'))
        or bool(c.get('market_data_available') or c.get('cn_market_data_available') or c.get('hk_market_data_available'))
    )
    if tool == 'search_library':
        return bool(c.get('search_library_available', True)) and (has_eligible_sources or can_acquire_sources)
    if tool in (
        'read_sources', 'read_pages', 'outline_source', 'search_source',
        'request_ocr', 'extract_statements', 'sec_filing_sections', 'sec_redline_diff',
    ):
        return has_eligible_sources or can_acquire_sources
    if tool in ('bank_fundamentals_diagnostics', 'bank_valuation_and_peer_compare'):
        return bool(c.get('sec_financials_available') or c.get('filings_available'))
    return tool in KNOWN_TOOLS


def packs_fingerprint() -> str:
    return hashlib.sha256('|'.join(pack_fingerprint(p) for _, p in sorted(_load().items())).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- SIC matching & L1+L2 composition

def _parse_sic_int(sic: str | int | None) -> tuple[int | None, str | None]:
    if sic is None or isinstance(sic, bool):
        return None, None
    raw = str(sic).strip()
    if not raw.isdigit():
        return None, None
    num = int(raw)
    if not (1 <= num <= 9999):
        return None, None
    return num, f'{num:04d}'


def classify_sic_sector(
    sic: str | int | None,
) -> tuple[str | None, Literal['non_financial', 'bank', 'insurance', 'reit', 'financial_other'], bool]:
    """Return ``(normalized_sic, sector_category, is_sector_gated)`` aligned with SEC SIC rules."""
    num, sic_code = _parse_sic_int(sic)
    if num is None or sic_code is None:
        return None, 'non_financial', False
    if 6000 <= num <= 6199:
        return sic_code, 'bank', True
    if 6300 <= num <= 6411:
        return sic_code, 'insurance', True
    if num == 6798:
        return sic_code, 'reit', True
    if 6200 <= num <= 6799:
        return sic_code, 'financial_other', True
    return sic_code, 'non_financial', False


def match_industry_pack_by_sic(sic: str | int | None) -> str | None:
    """Deterministically map a US SEC SIC code to one L2 non-financial KPI pack id, or ``None``."""
    num, _ = _parse_sic_int(sic)
    if num is None:
        return None
    if 6000 <= num <= 6799:
        return None
    for low, high, pack_id in SIC_INDUSTRY_RANGES:
        if low <= num <= high:
            return pack_id
    return None


# ---------------------------------------------------------------- selection

def _normalise(text: str) -> str:
    return unicodedata.normalize('NFKC', text).casefold()


def _term_matches(term: str, text: str) -> bool:
    term = _normalise(term)
    if term.isascii():
        return re.search(rf'(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])', text) is not None
    return term in text


def select_packs(question: str, *, max_packs: int = 2) -> list[SelectionResult]:
    """Deterministic, conservative selection. A pack needs a total trigger weight
    of at least MIN_SELECT_SCORE; ties break by pack id. No match -> empty list."""
    text = _normalise(question or '')
    results: list[SelectionResult] = []
    for pack_id, pack in _load().items():
        matched: list[str] = []
        score = 0
        for trig in pack.applies_when.triggers:
            if _term_matches(trig.term, text):
                matched.append(trig.term)
                score += trig.weight
        if score >= MIN_SELECT_SCORE:
            results.append(SelectionResult(pack_id=pack_id, score=score, matched_terms=sorted(matched)))
    results.sort(key=lambda r: (-r.score, r.pack_id))
    return results[:max(0, max_packs)]


# ---------------------------------------------------------------- rendering

def _tool_available(tool: str, caps: Mapping[str, Any]) -> bool:
    flag = CAPABILITY_FLAG.get(tool)
    if flag is None:
        return tool not in OPTIONAL_TOOLS
    if flag in caps:
        return bool(caps[flag])
    return tool not in OPTIONAL_TOOLS and not (tool == 'market_data')


def _capability_line(used_tools: Sequence[str], caps: Mapping[str, Any]) -> str | None:
    used = sorted(set(used_tools))
    missing = [t for t in used if not _tool_available(t, caps)]
    if not missing:
        return None
    line = 'Unavailable now (marked [n/a]): ' + ', '.join(missing) + '.'
    if 'market_data' in missing:
        if caps.get('market_data_key_required'):
            line += (" market_data needs the user's own market-data key (Settings); until then any price"
                     ' from news or filings is NOT market data: label it so.')
        else:
            line += ' market_data is unavailable in this run; label any price from news or filings as NOT market data.'
    return line


def _bound_lines(lines: Sequence[str], max_chars: int) -> str:
    text = '\n'.join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 16].rstrip() + ' ...[truncated]'
    return text


def _render_l1_pack(pack: Pack, *, available_capabilities: Mapping[str, Any] | None = None,
                    max_chars: int = MAX_PACK_CHARS) -> str:
    caps = available_capabilities or {}
    lines: list[str] = [
        f'[METHOD PACK {pack_fingerprint(pack)}: {pack.title_en} / {pack.title_zh}] '
        'Advisory method; the user question, research clock and tool results take priority.',
        'Applies to: ' + '; '.join(pack.applies_when.question_types),
    ]
    cap_line = _capability_line([t for r in pack.evidence_requirements for t in r.preferred_tools], caps)
    if cap_line:
        lines.append(cap_line)
    lines.append('Evidence plan (ordered; [R]=required):')
    for i, req in enumerate(pack.evidence_requirements, 1):
        tools = [t if _tool_available(t, caps) else f'{t}[n/a]' for t in req.preferred_tools]
        usable = [t for t in req.preferred_tools if _tool_available(t, caps)]
        flag = '[R]' if req.required else '[O]'
        lines.append(f'{i}. {flag} {req.id}: {req.description} Tools: {" > ".join(tools)}. '
                     f'Fresh: {req.freshness}. Accept: {req.acceptance}')
        if not req.preferred_tools:
            lines[-1] = lines[-1].replace('Tools: .', 'Tools: none (reasoning step).')
        elif not usable:
            lines.append(f'   FALLBACK: {req.fallback}')
        elif 'market_data' in req.preferred_tools and 'market_data' not in usable:
            lines.append(f'   Without market_data: {req.fallback}')
    ce = pack.counter_evidence
    lines.append(f'Counter-evidence (>= {ce.min_search_actions} dedicated search action; look for): '
                 + '; '.join(ce.look_for) + f'. If none found: {ce.if_none_found}')
    lines.append('Checks: ' + '; '.join(pack.unit_and_period_checks))
    if pack.reconciliation:
        lines.append('Reconcile: ' + '; '.join(
            f'{r.metric} [{" vs ".join(r.source_classes)}] {r.note}' for r in pack.reconciliation))
    lines.append('Stop when: ' + '; '.join(pack.stopping_conditions))
    rs = pack.report_structure
    lines.append('Report: ' + ' | '.join(rs.sections) + '. Must state: ' + '; '.join(rs.must_state))
    if pack.limits:
        lines.append('Limits: ' + '; '.join(pack.limits))
    return _bound_lines(lines, max_chars)


def render_industry_pack_for_prompt(
    pack: IndustryKpiPack,
    *,
    available_capabilities: Mapping[str, Any] | None = None,
    max_chars: int = MAX_INDUSTRY_PACK_CHARS,
) -> str:
    """Render an L2 industry KPI pack deterministically within ``max_chars``."""
    caps = available_capabilities or {}
    lines: list[str] = [
        f'[L2 INDUSTRY KPI PACK {pack_fingerprint(pack)}: {pack.title_en} / {pack.title_zh}] '
        f'Scope: {pack.sector_scope}.',
    ]
    cap_line = _capability_line([t for k in pack.kpis for t in k.preferred_tools], caps)
    if cap_line:
        lines.append(cap_line)
    lines.append('Industry KPIs ([R]=required):')
    for idx, kpi in enumerate(pack.kpis, 1):
        tools = [t if _tool_available(t, caps) else f'{t}[n/a]' for t in kpi.preferred_tools]
        flag = '[R]' if kpi.required else '[O]'
        lines.append(
            f'{idx}. {flag} {kpi.id} ({kpi.name_en} / {kpi.name_zh}): {kpi.definition_and_formula}. '
            f'Tools: {" > ".join(tools)}. Where: {kpi.where_to_look}. Red flags: {"; ".join(kpi.red_flags)}'
        )
    lines.append('Filing focus: ' + '; '.join(pack.filing_focus))
    lines.append(
        'Anchored bear angles: ' + '; '.join(
            f'{a.angle} [anchor: {a.evidence_anchor}; trigger: {a.next_quarter_trigger}]'
            for a in pack.counter_evidence_angles
        )
    )
    lines.append('Industry checks: ' + '; '.join(pack.unit_and_period_checks))
    if pack.reconciliation:
        lines.append('Industry reconcile: ' + '; '.join(
            f'{r.metric} [{" vs ".join(r.source_classes)}] {r.note}' for r in pack.reconciliation
        ))
    if pack.limits:
        lines.append('Industry limits: ' + '; '.join(pack.limits))
    return _bound_lines(lines, max_chars)


def _render_composed_prompt(
    base_pack: Pack,
    industry_pack: IndustryKpiPack | None,
    *,
    sic_code: str | None,
    sector_category: str,
    is_sector_gated: bool,
    available_capabilities: Mapping[str, Any] | None = None,
    max_chars: int = MAX_COMPOSED_PACK_CHARS,
) -> str:
    l1_budget = MAX_PACK_CHARS if industry_pack is not None else min(max_chars, MAX_PACK_CHARS)
    parts = [_render_l1_pack(base_pack, available_capabilities=available_capabilities, max_chars=l1_budget)]
    if is_sector_gated:
        parts.append(
            f'[SECTOR GATE: SEC SIC {sic_code or "?"} ({sector_category}) — '
            'non-financial L2 KPI packs and industrial metrics (gross margin, inventory turnover, standard EV/EBITDA, '
            'standard FCF, Reverse-DCF) are disabled; apply sector-specific metrics only.]'
        )
    if industry_pack is not None:
        used_chars = sum(len(p) for p in parts) + len(parts)
        remaining = max(512, max_chars - used_chars)
        parts.append(
            render_industry_pack_for_prompt(
                industry_pack,
                available_capabilities=available_capabilities,
                max_chars=min(MAX_INDUSTRY_PACK_CHARS, remaining),
            )
        )
    return _bound_lines(parts, max_chars)


def render_for_prompt(
    pack: Pack | IndustryKpiPack | ComposedEarningsMethodPack,
    *,
    available_capabilities: Mapping[str, Any] | None = None,
    max_chars: int | None = None,
) -> str:
    if isinstance(pack, IndustryKpiPack):
        return render_industry_pack_for_prompt(
            pack,
            available_capabilities=available_capabilities,
            max_chars=MAX_INDUSTRY_PACK_CHARS if max_chars is None else max_chars,
        )
    if isinstance(pack, ComposedEarningsMethodPack):
        return _render_composed_prompt(
            pack.base_pack,
            pack.industry_pack,
            sic_code=pack.sic_code,
            sector_category=pack.sector_category,
            is_sector_gated=pack.is_sector_gated,
            available_capabilities=available_capabilities,
            max_chars=MAX_COMPOSED_PACK_CHARS if max_chars is None else max_chars,
        )
    return _render_l1_pack(
        pack,
        available_capabilities=available_capabilities,
        max_chars=MAX_PACK_CHARS if max_chars is None else max_chars,
    )


def compose_earnings_method_pack(
    sic: str | int | None = None,
    industry_pack_id: str | None = None,
    *,
    active_sector_plugin: str | None = None,
    available_capabilities: Mapping[str, Any] | None = None,
    max_chars: int = MAX_COMPOSED_PACK_CHARS,
) -> ComposedEarningsMethodPack:
    """Compose the L1 ``earnings_review`` backbone with a deterministic L2 industry KPI pack.

    Selection precedence:
    1. If ``sic`` falls in a gated financial sector (Banks, Insurance, REITs, or 6200-6799):
       - When ``active_sector_plugin == 'us_large_bank_v4a'``, keep ``is_sector_gated=True``
         and layer the specialized ``kpi_us_large_banks`` L2 pack (``selection_source='sector_plugin'``),
         while rejecting non-bank overrides (e.g., ``kpi_saas_cloud``).
       - Otherwise, all L2 packs remain blocked (explicit override raises ``ValueError``;
         auto-match returns ``industry_pack=None`` with ``is_sector_gated=True``).
    2. Otherwise (non-financial SIC), bank packs are rejected (e.g., ``7372 + kpi_us_large_banks``),
       and an explicit non-financial ``industry_pack_id`` overrides SIC auto-matching.
    3. Otherwise, ``match_industry_pack_by_sic(sic)`` deterministically selects the L2 pack (or ``None``).
    """
    base_pack = get_pack('earnings_review')
    if base_pack is None:
        raise RuntimeError('Base earnings_review method pack is missing.')

    sic_code, sector_category, is_sector_gated = classify_sic_sector(sic)
    matched_id = match_industry_pack_by_sic(sic)

    cleaned_override = industry_pack_id.strip() if isinstance(industry_pack_id, str) and industry_pack_id.strip() else None
    if industry_pack_id is not None and not isinstance(industry_pack_id, str):
        raise ValueError('industry_pack_id must be a string when provided.')

    industry_pack: IndustryKpiPack | None = None
    selection_source: Literal['none', 'sic_auto', 'explicit_override', 'sector_gated', 'sector_plugin'] = 'none'

    if is_sector_gated:
        if active_sector_plugin == 'us_large_bank_v4a':
            if cleaned_override is not None and cleaned_override != 'kpi_us_large_banks':
                raise ValueError(
                    f'Cannot apply non-financial industry pack {cleaned_override!r} to bank plugin SEC SIC '
                    f'{sic_code} ({sector_category}).'
                )
            industry_pack = get_industry_pack('kpi_us_large_banks')
            if industry_pack is None:
                raise RuntimeError('Matched bank plugin pack kpi_us_large_banks could not be loaded.')
            selection_source = 'sector_plugin'
        else:
            if cleaned_override is not None:
                raise ValueError(
                    f'Cannot apply non-financial industry pack {cleaned_override!r} to gated financial SEC SIC '
                    f'{sic_code} ({sector_category}).'
                )
            selection_source = 'sector_gated'
    elif cleaned_override is not None:
        if sic_code is not None and cleaned_override == 'kpi_us_large_banks':
            raise ValueError(
                f'Cannot apply financial industry pack {cleaned_override!r} to non-financial SEC SIC '
                f'{sic_code}.'
            )
        industry_pack = get_industry_pack(cleaned_override)
        if industry_pack is None:
            available = sorted(_load_industry_packs())
            raise ValueError(f'Unknown industry KPI pack {cleaned_override!r}. Available: {", ".join(available)}.')
        selection_source = 'explicit_override'
    elif matched_id is not None:
        industry_pack = get_industry_pack(matched_id)
        if industry_pack is None:
            raise RuntimeError(f'Matched industry pack {matched_id!r} could not be loaded.')
        selection_source = 'sic_auto'

    base_fp = pack_fingerprint(base_pack)
    pack_records: list[dict[str, Any]] = [
        {'pack_id': base_pack.id, 'version': base_pack.version, 'fingerprint': base_fp, 'layer': 'l1'},
    ]
    if industry_pack is not None:
        l2_fp = pack_fingerprint(industry_pack)
        fingerprint = f'{base_fp}+{l2_fp}'
        pack_records.append({
            'pack_id': industry_pack.id,
            'version': industry_pack.version,
            'fingerprint': l2_fp,
            'layer': industry_pack.layer,
        })
    else:
        fingerprint = base_fp

    merged_checks = _dedup_preserve([
        *base_pack.unit_and_period_checks,
        *(industry_pack.unit_and_period_checks if industry_pack is not None else ()),
    ])
    merged_reconciliation = [
        *base_pack.reconciliation,
        *(industry_pack.reconciliation if industry_pack is not None else ()),
    ]
    merged_bear_look_for = _dedup_preserve([
        *base_pack.counter_evidence.look_for,
        *(
            f'{a.angle} (anchor: {a.evidence_anchor}; trigger: {a.next_quarter_trigger})'
            for a in (industry_pack.counter_evidence_angles if industry_pack is not None else ())
        ),
    ])
    merged_limits = _dedup_preserve([
        *base_pack.limits,
        *(industry_pack.limits if industry_pack is not None else ()),
    ])
    rendered = _render_composed_prompt(
        base_pack,
        industry_pack,
        sic_code=sic_code,
        sector_category=sector_category,
        is_sector_gated=is_sector_gated,
        available_capabilities=available_capabilities,
        max_chars=max_chars,
    )
    return ComposedEarningsMethodPack(
        base_pack=base_pack,
        industry_pack=industry_pack,
        sic_code=sic_code,
        matched_industry_pack_id=matched_id,
        industry_pack_id=industry_pack.id if industry_pack is not None else None,
        sector_category=sector_category,
        is_sector_gated=is_sector_gated,
        active_sector_plugin=active_sector_plugin if selection_source == 'sector_plugin' else None,
        selection_source=selection_source,
        fingerprint=fingerprint,
        pack_records=pack_records,
        required_blocks=list(base_pack.report_structure.sections),
        unit_and_period_checks=merged_checks,
        reconciliation=merged_reconciliation,
        counter_evidence_look_for=merged_bear_look_for,
        limits=merged_limits,
        rendered_prompt=rendered,
    )
