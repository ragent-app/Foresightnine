"""Conservative, auditable cross-source numeric comparison."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from pydantic import ValidationError

from .research_calculations import NumericFact, _is_ratio_scale_factor
from .research_analysis import _quoted_numbers
from .research_sources import ResearchSource


_PERCENT = re.compile(r"(?<![A-Za-z0-9_.])([-−]?\d+(?:\.\d+)?)\s*(?:%|％|percent\b)", re.IGNORECASE)
_ZH_USD_AMOUNT = re.compile(r"(?<![\d,.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(万亿|千万|百万|十万|亿|万|千)?\s*(美元|美金)")
_ZH_USD_TO_MILLIONS = {"万亿": Decimal("1000000"), "亿": Decimal("100"), "千万": Decimal("10"),
                       "百万": Decimal("1"), "十万": Decimal("0.1"), "万": Decimal("0.01"),
                       "千": Decimal("0.001"), "": Decimal("0.000001")}
_ZH_PUBLICATION = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月(?:\s*(3[01]|[12]\d|0?[1-9])\s*日)?\s*(?:发布|公布|发表|刊登)")
_EN_PUBLICATION = re.compile(r"\b(?:published|released|issued)\s+(?:on|in)\s+((?:19|20)\d{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?(?![-\d])", re.IGNORECASE)
_RATE_NUMBER = r"\d+(?:-\d+/\d+|\.\d+)?"
_RATE_LEVEL_POINTS = re.compile(
    rf"(?:利率目标区间|目标利率区间|联邦基金利率目标区间|利率区间|目标区间|"
    rf"(?:interest\s+rate|federal\s+funds\s+rate|target)\s+(?:target\s+)?range)"
    rf"(?P<between>[^。！？.!?;；\n]{{0,48}}?)(?P<range>{_RATE_NUMBER}\s*(?:至|到|to|–|—)\s*{_RATE_NUMBER})\s*(?:个百分点|percentage\s+points?)",
    re.IGNORECASE)
_RATE_CHANGE = re.compile(r"上调|下调|上升|下降|扩大|缩小|增加|减少|升高|降低|调整|"
                          r"\b(?:adjust|increase|decrease|narrow|widen|change|cut|rise|raise|lower)\w*\b", re.IGNORECASE)


def flag_rate_level_point_warnings(claims: list[dict]) -> list[dict]:
    """Flag a narrow possible level-versus-change unit confusion, not a verdict."""
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("rate_unit_warnings", None)
        text = entry.get("text")
        if isinstance(text, str) and len(text) <= 3000:
            warnings = [{"claimed_text": match.group(0), "status": "requires_review"}
                        for match in _RATE_LEVEL_POINTS.finditer(text)
                        if not _RATE_CHANGE.search(match.group("between"))][:5]
            if warnings:
                entry["rate_unit_warnings"] = warnings
        output.append(entry)
    return output


def flag_publication_date_mismatches(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Flag explicit publication dates that disagree with one cited source's declared date.

    Date declarations are unverified, and the text may refer to another
    publication. This is a narrow review candidate, not an error verdict.
    """
    source_map = {item.source_id: item for item in sources if item.published_at is not None}
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("publication_date_warnings", None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        cited = {item.get("source_id") for item in citations if isinstance(item, dict) and isinstance(item.get("source_id"), str)}
        if len(cited) != 1 or (source := source_map.get(next(iter(cited)))) is None:
            output.append(entry)
            continue
        declared = source.published_at.date()
        warnings = []
        for pattern in (_ZH_PUBLICATION, _EN_PUBLICATION):
            for match in pattern.finditer(text):
                year, month, day = match.groups()
                try:
                    actual = date(int(year), int(month), int(day or 1))
                except ValueError:
                    continue
                if (actual.year, actual.month) != (declared.year, declared.month) or (day is not None and actual.day != declared.day):
                    warnings.append({"claimed_text": match.group(0), "source_id": source.source_id,
                                     "declared_published_at": source.published_at.isoformat(), "status": "requires_review"})
                if len(warnings) >= 5:
                    break
            if len(warnings) >= 5:
                break
        if warnings:
            entry["publication_date_warnings"] = warnings
        output.append(entry)
    return output


def claim_amount_differences(claims: list[dict], facts: list[dict]) -> list[dict]:
    """Flag unmatched Chinese USD amounts only when another amount anchors the same cited facts.

    This is a review candidate, not a finding that a claim is false. It covers
    explicit Chinese USD units only, preserves rounding, and fails closed when
    model facts or citations are missing or ambiguous.
    """
    usable: list[tuple[str, str, Decimal]] = []
    for item in facts:
        source_id, fact_id = item.get("source_id"), item.get("fact_id")
        quote, location = item.get("quotation"), item.get("location")
        unit = " ".join(str(item.get("unit") or "").casefold().split())
        currency = str(item.get("currency") or "").upper()
        if (item.get("verification") != "model_extraction_not_independently_verified" or item.get("authorship") == "user" or
                not isinstance(source_id, str) or not isinstance(fact_id, str) or not isinstance(quote, str) or
                not isinstance(location, dict) or location.get("status") == "ambiguous" or
                type(location.get("character_start")) is not int or type(location.get("character_end")) is not int or
                location["character_start"] < 0 or location["character_end"] - location["character_start"] != len(quote) or
                not ("million" in unit and ("usd" in unit or currency == "USD")) or currency not in {"", "USD"}):
            continue
        try:
            value = Decimal(str(item.get("value")))
            if not value.is_finite() or value not in _quoted_numbers(quote):
                continue
        except (InvalidOperation, ValueError):
            continue
        usable.append((source_id, fact_id, value))
    result: list[dict] = []
    for index, claim in enumerate(claims):
        text, citations = claim.get("text"), claim.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            continue
        cited = {c.get("source_id") for c in citations if isinstance(c, dict) and isinstance(c.get("source_id"), str)}
        candidates = [(fact_id, value) for source_id, fact_id, value in usable if source_id in cited]
        matches = list(_ZH_USD_AMOUNT.finditer(text))
        if not candidates or len(matches) < 2 or len(matches) > 20:
            continue
        checked = []
        for match in matches:
            raw = match.group(1).replace(",", "").replace("−", "-")
            precision = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
            if len(raw) > 40 or precision > 8:
                checked = []
                break
            try:
                amount = Decimal(raw); factor = _ZH_USD_TO_MILLIONS[match.group(2) or ""]
                with localcontext() as context:
                    context.prec = 100
                    matches_fact = any((value / factor).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP) == amount
                                       for _, value in candidates)
            except (InvalidOperation, ValueError):
                checked = []
                break
            checked.append((match, matches_fact))
        if not checked or not any(matched for _, matched in checked):
            continue
        for match, matched in checked:
            if not matched:
                result.append({"claim_index": index, "claimed_amount": match.group(0),
                               "source_fact_ids": list(dict.fromkeys(fact_id for fact_id, _ in candidates))[:20],
                               "status": "requires_review"})
                if len(result) == 50:
                    return result
    return result


def suggest_percentage_evidence(claims: list[dict], facts: list[dict]) -> list[dict]:
    """Suggest uniquely matched facts as candidates, never verified support.

    Only source-backed, unedited facts with unique values and exact locations
    qualify. Identical values, stale/user facts and ambiguous locations fail
    closed. A numeric match cannot establish entity, period or entailment.
    """
    by_value: dict[Decimal, list[dict]] = defaultdict(list)
    for item in facts:
        location, quote = item.get("location"), item.get("quotation")
        unit = str(item.get("unit") or "").casefold()
        if (item.get("verification") != "model_extraction_not_independently_verified" or item.get("authorship") == "user" or
                not isinstance(item.get("source_id"), str) or not isinstance(item.get("fact_id"), str) or
                not isinstance(quote, str) or not quote or len(quote) > 1000 or item.get("currency") is not None or
                not isinstance(location, dict) or location.get("status") == "ambiguous" or
                type(location.get("character_start")) is not int or type(location.get("character_end")) is not int or
                not (0 <= location["character_start"] < location["character_end"] <= location["character_start"] + 1000) or
                location["character_end"] - location["character_start"] != len(quote) or
                not (unit == "%" or re.match(r"^(?:percent|percentage)(?:\s*\(|$)", unit))):
            continue
        try:
            value = Decimal(str(item.get("value")))
            if not value.is_finite() or value not in _quoted_numbers(quote): continue
        except (InvalidOperation, ValueError):
            continue
        by_value[value].append(item)
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("suggested_fact_evidence", None)
        entry.pop("uncited_percentages", None)
        text = entry.get("text")
        citations = entry.get("citations")
        if not isinstance(text, str) or not isinstance(citations, list):
            output.append(entry)
            continue
        existing: set[Decimal] = set()
        for citation in citations:
            quote = citation.get("quotation") if isinstance(citation, dict) else None
            if isinstance(quote, str):
                existing.update(_quoted_numbers(quote))
        suggested = []
        uncited = []
        seen: set[Decimal] = set()
        for match in _PERCENT.finditer(text):
            if _is_ratio_scale_factor(text, match):
                continue
            try: value = Decimal(match.group(1).replace("−", "-"))
            except InvalidOperation: continue
            if value not in existing and match.group(0) not in uncited and len(uncited) < 5:
                uncited.append(match.group(0))
            candidates = by_value.get(value, [])
            if value in seen or value in existing or len(candidates) != 1:
                continue
            seen.add(value)
            fact = candidates[0]
            suggested.append({key: deepcopy(fact[key]) for key in
                              ("fact_id", "source_id", "value", "unit", "period", "quotation", "location") if key in fact} |
                             {"status": "requires_semantic_review"})
            if len(suggested) == 5: break
        if suggested:
            entry["suggested_fact_evidence"] = suggested
        if uncited:
            entry["uncited_percentages"] = uncited
        output.append(entry)
    return output


def evidence_differences(facts: list[dict]) -> list[dict]:
    """Flag matching labels/periods/units with different cross-source values.

    Identical labels do not establish an identical company, accounting basis,
    or independence of sources. These are review candidates, not conflicts.
    """
    counts: dict[str, int] = defaultdict(int)
    for item in facts:
        if isinstance(item.get("fact_id"), str):
            counts[item["fact_id"].strip()] += 1
    groups: dict[tuple[str, str, str, str | None], list[tuple[NumericFact, dict]]] = defaultdict(list)
    for item in facts:
        label, source, fact_id = item.get("label"), item.get("source_id"), item.get("fact_id")
        if (not isinstance(label, str) or not label.strip() or len(label) > 300 or
                not isinstance(source, str) or not source.strip() or len(source) > 100 or
                not isinstance(fact_id, str) or counts.get(fact_id.strip(), 0) != 1):
            continue
        try:
            fact = NumericFact.model_validate({key: item[key] for key in
                ("fact_id", "value", "unit", "currency", "period") if key in item})
        except (ValidationError, TypeError, ValueError):
            continue
        key = (" ".join(label.split()).casefold(), fact.period.casefold(),
               fact.unit.casefold(), fact.currency.casefold() if fact.currency else None)
        groups[key].append((fact, item))
    result = []
    for entries in groups.values():
        if len({item["source_id"] for _, item in entries}) < 2 or len({fact.value for fact, _ in entries}) < 2:
            continue
        result.append({"label": entries[0][1]["label"], "period": entries[0][0].period,
            "unit": entries[0][0].unit, "currency": entries[0][0].currency,
            "status": "requires_entity_and_method_review", "truncated": len(entries) > 20,
            "facts": [{"fact_id": fact.fact_id, "source_id": item["source_id"], "value": str(fact.value)}
                      for fact, item in entries[:20]]})
        if len(result) == 50:
            break
    return result
