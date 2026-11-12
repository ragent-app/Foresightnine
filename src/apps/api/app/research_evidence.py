"""Conservative, auditable cross-source numeric comparison."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

from pydantic import ValidationError

from .research_calculations import NumericFact, _is_ratio_scale_factor
from .research_analysis import _quoted_numbers


_PERCENT = re.compile(r"(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(?:%|％|percent\b)", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


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
        text = entry.get("text")
        citations = entry.get("citations")
        if not isinstance(text, str) or not isinstance(citations, list):
            output.append(entry)
            continue
        existing: set[Decimal] = set()
        for citation in citations:
            quote = citation.get("quotation") if isinstance(citation, dict) else None
            if isinstance(quote, str):
                for match in _NUMBER.finditer(quote):
                    try: existing.add(Decimal(match.group().replace(",", "").replace("−", "-")))
                    except InvalidOperation: pass
        suggested = []
        seen: set[Decimal] = set()
        for match in _PERCENT.finditer(text):
            if _is_ratio_scale_factor(text, match):
                continue
            try: value = Decimal(match.group(1).replace("−", "-"))
            except InvalidOperation: continue
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
