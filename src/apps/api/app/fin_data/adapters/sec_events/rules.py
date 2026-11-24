"""Externalized rules loader and Pydantic schema validation for V1 SEC events."""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ....research_signals import ThesisPillar

DEFAULT_RULES_PATH = Path(__file__).with_name("sec_events_rules.json")
OfficerTier = Literal[
    "ceo_cfo_chair",
    "c_suite_evp",
    "vp_other_officer",
    "director",
    "ten_pct_owner",
]
FALLBACK_EVENT_SUBTYPE = "other_material"


class _RulesModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ScanLimitsRules(_RulesModel):
    default_lookback_days: int = Field(default=90, ge=1, le=730)
    recent_context_days: int = Field(default=30, ge=0, le=180)
    baseline_lookback_days: int = Field(default=365, ge=30, le=1095)
    max_target_candidates: int = Field(default=18, ge=1, le=200)
    max_recent_context_candidates: int = Field(default=10, ge=0, le=50)
    max_submissions_pages: int = Field(default=5, ge=0, le=50)
    cluster_window_days: int = Field(default=14, ge=1, le=90)
    exhibit_lazy_max_bytes: int = Field(default=2_500_000, ge=100_000, le=3_000_000)
    exhibit_lazy_hard_cap_bytes: int = Field(default=3_000_000, ge=100_000, le=3_145_728)
    source_texts_total_max_chars: int = Field(default=6_000_000, ge=100_000, le=20_000_000)


class Form8kRules(_RulesModel):
    earnings_only_items: list[str] = Field(default_factory=lambda: ["2.02"])
    earnings_companion_items: list[str] = Field(default_factory=lambda: ["9.01", "7.01"])
    low_materiality_only_items: list[str] = Field(default_factory=lambda: ["5.07", "5.03", "3.02", "3.03", "9.01"])
    statutory_critical_items: list[str] = Field(default_factory=lambda: ["1.03", "2.04", "4.02"])
    keep_all_items: list[str] = Field(default_factory=lambda: ["8.01", "5.02"])
    ex99_max_excerpt_chars: int = Field(default=12_000, ge=500, le=100_000)
    primary_max_excerpt_chars: int = Field(default=16_000, ge=500, le=100_000)
    ex99_high_density_keywords: list[str] = Field(default_factory=list)


class Form4Rules(_RulesModel):
    keep_transaction_codes: list[str] = Field(default_factory=lambda: ["P", "S"])
    exclude_transaction_codes: list[str] = Field(default_factory=list)
    dust_floor_usd: Decimal = Field(default=Decimal("25000"), ge=Decimal("0"))
    exempt_missing_price_from_dust_floor: bool = True
    exclude_confirmed_10b5_1: bool = True
    cluster_window_days: int = Field(default=14, ge=1, le=90)
    plan_10b5_1_positive_patterns: list[str] = Field(default_factory=list)
    plan_10b5_1_negative_patterns: list[str] = Field(default_factory=list)
    officer_title_tiers: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("keep_transaction_codes", "exclude_transaction_codes")
    @classmethod
    def _upper_codes(cls, values: list[str]) -> list[str]:
        return [v.strip().upper() for v in values if v and v.strip()]

    @field_validator("plan_10b5_1_positive_patterns", "plan_10b5_1_negative_patterns")
    @classmethod
    def _validate_regexes(cls, patterns: list[str]) -> list[str]:
        for pat in patterns:
            re.compile(pat, re.IGNORECASE)
        return patterns


class Form13dgRules(_RulesModel):
    supported_forms: list[str] = Field(default_factory=list)
    passive_reporting_person_types: list[str] = Field(default_factory=lambda: ["IA", "IC", "BK"])
    passive_delta_threshold_pp: Decimal = Field(default=Decimal("1.0"), ge=Decimal("0"))
    passive_initial_13g_suppress_below_pct: Decimal = Field(default=Decimal("10.0"), ge=Decimal("0"))
    passive_institution_whitelist: list[str] = Field(default_factory=list)

    @field_validator("supported_forms", "passive_reporting_person_types")
    @classmethod
    def _upper_list(cls, values: list[str]) -> list[str]:
        return [v.strip().upper() for v in values if v and v.strip()]


class Routine6kPattern(_RulesModel):
    rule_id: str
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _compile(cls, value: str) -> str:
        re.compile(value, re.IGNORECASE)
        return value


class Form6kRules(_RulesModel):
    fpi_indicator_forms: list[str] = Field(default_factory=lambda: ["20-F", "20-F/A", "6-K", "6-K/A"])
    earnings_patterns: list[str] = Field(default_factory=list)
    routine_noise_patterns: list[Routine6kPattern] = Field(default_factory=list)

    @field_validator("fpi_indicator_forms")
    @classmethod
    def _upper_fpi(cls, values: list[str]) -> list[str]:
        return [v.strip().upper() for v in values if v and v.strip()]

    @field_validator("earnings_patterns")
    @classmethod
    def _compile_earnings(cls, patterns: list[str]) -> list[str]:
        for pat in patterns:
            re.compile(pat, re.IGNORECASE)
        return patterns


class EventSubtypeRule(_RulesModel):
    description: str
    affected_thesis_pillars: list[ThesisPillar] = Field(default_factory=list)


class SecEventsRules(_RulesModel):
    rules_version: str = "v1"
    parser_version: str = "v1"
    limits: ScanLimitsRules = Field(default_factory=ScanLimitsRules)
    form8k: Form8kRules = Field(default_factory=Form8kRules)
    form4: Form4Rules = Field(default_factory=Form4Rules)
    form13dg: Form13dgRules = Field(default_factory=Form13dgRules)
    form6k: Form6kRules = Field(default_factory=Form6kRules)
    negation_markers: list[str] = Field(default_factory=list)
    event_subtypes: dict[str, EventSubtypeRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_subtypes(self) -> "SecEventsRules":
        if len(self.event_subtypes) != 16:
            raise ValueError(f"Expected 16 canonical event_subtypes, got {len(self.event_subtypes)}.")
        if FALLBACK_EVENT_SUBTYPE not in self.event_subtypes:
            raise ValueError(f"Missing required fallback subtype `{FALLBACK_EVENT_SUBTYPE}`.")
        for keep_item in self.form8k.keep_all_items:
            if keep_item in self.form8k.low_materiality_only_items or keep_item in self.form8k.earnings_only_items:
                raise ValueError(f"8-K keep_all_items `{keep_item}` cannot also be excluded.")
        return self


@lru_cache(maxsize=4)
def _load_cached_rules(path_str: str) -> SecEventsRules:
    raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
    return SecEventsRules.model_validate(raw)


def load_sec_events_rules(
    rules_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
) -> SecEventsRules:
    """Load and Pydantic-validate ``sec_events_rules.json`` (optionally applying test overrides)."""
    resolved = Path(rules_path) if rules_path is not None else DEFAULT_RULES_PATH
    base = _load_cached_rules(str(resolved.resolve()))
    if not overrides:
        return base
    merged = base.model_dump(mode="python")
    _deep_merge(merged, overrides)
    return SecEventsRules.model_validate(merged)


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], val)
        else:
            target[key] = val


def normalize_event_subtype(rules: SecEventsRules, subtype: str | None) -> str:
    """Map unknown/legacy subtypes safely to ``other_material``."""
    cand = (subtype or "").strip()
    if cand in rules.event_subtypes:
        return cand
    return FALLBACK_EVENT_SUBTYPE


def resolve_affected_pillars(rules: SecEventsRules, subtype: str | None) -> list[ThesisPillar]:
    """Resolve Direction B thesis pillars from an ``event_subtype`` via externalized rules."""
    canonical = normalize_event_subtype(rules, subtype)
    rule = rules.event_subtypes.get(canonical) or rules.event_subtypes[FALLBACK_EVENT_SUBTYPE]
    return list(rule.affected_thesis_pillars)


def get_rules_taxonomy_subset_hash(rules: SecEventsRules) -> str:
    """Hash ONLY the 16-subtype taxonomy subset injected into Layer 2 prompts.

    Modifying Layer 1 thresholds, passive institution lists, or Layer 3 negation markers
    never changes this hash, preserving Layer 2 semantic cache hits.
    """
    payload = {
        key: {
            "description": rule.description,
            "affected_thesis_pillars": list(rule.affected_thesis_pillars),
        }
        for key, rule in sorted(rules.event_subtypes.items())
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()[:16]


__all__ = [
    "DEFAULT_RULES_PATH",
    "EventSubtypeRule",
    "FALLBACK_EVENT_SUBTYPE",
    "Form13dgRules",
    "Form4Rules",
    "Form6kRules",
    "Form8kRules",
    "OfficerTier",
    "Routine6kPattern",
    "ScanLimitsRules",
    "SecEventsRules",
    "get_rules_taxonomy_subset_hash",
    "load_sec_events_rules",
    "normalize_event_subtype",
    "resolve_affected_pillars",
]
