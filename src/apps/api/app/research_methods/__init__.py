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
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PACKS_DIR = Path(__file__).parent / 'packs'
MAX_PACK_CHARS = 3500
MIN_SELECT_SCORE = 2

# Tools a pack may name. search_library / news_search are optional and may be
# absent in a given run; the renderer marks anything unavailable.
KNOWN_TOOLS = frozenset({
    'resolve_entity', 'market_data', 'company_financials', 'filings',
    'acquire_sources', 'read_sources', 'read_pages', 'outline_source', 'search_source',
    'request_ocr', 'record_facts', 'calculate_financials', 'create_chart', 'submit_research',
    'search_library', 'news_search', 'issuer_documents',
})
OPTIONAL_TOOLS = frozenset({'search_library', 'news_search'})
# tool -> capability flag in the run's available_capabilities dict
CAPABILITY_FLAG = {
    'market_data': 'market_data_available',
    'company_financials': 'sec_financials_available',
    'filings': 'filings_available',
    'acquire_sources': 'acquire_available',
    'search_library': 'search_library_available',
    'news_search': 'news_search_available',
    'issuer_documents': 'issuer_documents_available',
}


class _Model(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


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
        unknown = [t for t in tools if t not in KNOWN_TOOLS]
        if unknown:
            raise ValueError(f'unknown tools: {unknown}')
        if len(set(tools)) != len(tools):
            raise ValueError('duplicate tools')
        return tools


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


class SelectionResult(_Model):
    pack_id: str
    score: int
    matched_terms: list[str]


def pack_fingerprint(pack: Pack) -> str:
    """Stable id for run records: ``<id>@<version>+<sha256[:12] of canonical content>``."""
    canonical = json.dumps(pack.model_dump(mode='json'), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]
    return f'{pack.id}@{pack.version}+{digest}'


@lru_cache(maxsize=1)
def _load() -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    for path in sorted(PACKS_DIR.glob('*.json')):
        pack = Pack.model_validate_json(path.read_text(encoding='utf-8'))
        if pack.id != path.stem:
            raise ValueError(f'pack id {pack.id!r} does not match file {path.name}')
        packs[pack.id] = pack
    return packs


def load_packs() -> dict[str, Pack]:
    return dict(_load())


def packs_fingerprint() -> str:
    return hashlib.sha256('|'.join(pack_fingerprint(p) for _, p in sorted(_load().items())).encode()).hexdigest()[:12]


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


def _capability_line(pack: Pack, caps: Mapping[str, Any]) -> str | None:
    used = sorted({t for r in pack.evidence_requirements for t in r.preferred_tools})
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


def render_for_prompt(pack: Pack, *, available_capabilities: Mapping[str, Any] | None = None,
                      max_chars: int = MAX_PACK_CHARS) -> str:
    caps = available_capabilities or {}
    lines: list[str] = [
        f'[METHOD PACK {pack_fingerprint(pack)}: {pack.title_en} / {pack.title_zh}] '
        'Advisory method; the user question, research clock and tool results take priority.',
        'Applies to: ' + '; '.join(pack.applies_when.question_types),
    ]
    cap_line = _capability_line(pack, caps)
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
    text = '\n'.join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 16].rstrip() + ' ...[truncated]'
    return text
