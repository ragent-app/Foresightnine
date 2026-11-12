"""Narrow mechanical checks for future two-source comparison evaluations.

These checks identify missing numeric quotations and explicit Chinese USD unit
mismatches. They do not establish entity, temporal or semantic entailment.
"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

_AMOUNT = re.compile(r"(?<![\d,.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:个\s*)?(万亿|千亿|百亿|十亿|千万|百万|十万|亿|万|千|百)?\s*(?:美元|美金)")
_FACTORS = {"万亿": Decimal("1000000"), "千亿": Decimal("100000"), "百亿": Decimal("10000"),
            "十亿": Decimal("1000"), "亿": Decimal("100"), "千万": Decimal("10"),
            "百万": Decimal("1"), "十万": Decimal("0.1"), "万": Decimal("0.01"),
            "千": Decimal("0.001"), "百": Decimal("0.0001"), "": Decimal("0.000001")}


def _contains_number(text: str, token: str) -> bool:
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", token):
        return False
    return bool(re.search(r"(?<![\d.,])" + re.escape(token) + r"(?![\d.])", text))


def comparison_quotations_cover_values(claim: dict, sources: dict[str, str], expected: dict[str, str]) -> bool:
    """Require each expected value in the claim and in its own source quotation."""
    text, citations = claim.get("text"), claim.get("citations")
    if not isinstance(text, str) or not isinstance(citations, list) or not expected or claim.get("support_status") == "incomplete":
        return False
    for source_id, value in expected.items():
        if not isinstance(value, str) or not _contains_number(text, value):
            return False
        source = sources.get(source_id)
        if not isinstance(source, str):
            return False
        covered = False
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("source_id") != source_id or citation.get("partial_token"):
                continue
            q, loc = citation.get("quotation"), citation.get("location")
            if not isinstance(q, str) or not isinstance(loc, dict) or loc.get("status") == "ambiguous":
                continue
            a, b = loc.get("character_start"), loc.get("character_end")
            if (type(a) is int and type(b) is int and 0 <= a < b <= len(source) and
                    source[a:b] == q and _contains_number(q, value)):
                covered = True
                break
        if not covered:
            return False
    return True


def unexpected_explicit_usd_amounts(text: str, allowed_millions: list[str]) -> list[str]:
    """Flag explicit Chinese USD amounts not matching references after rounding.

    Bare numbers and other currencies are outside this check. Callers must
    pre-register the reference values and decide whether their absence is fatal.
    """
    try:
        allowed = [Decimal(v) for v in allowed_millions]
        if not allowed or any(not x.is_finite() for x in allowed):
            raise ValueError("References must be finite and nonempty")
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid references") from error
    bad = []
    for match in _AMOUNT.finditer(text):
        raw = match.group(1).replace(",", "").replace("−", "-")
        precision = len(raw.split(".", 1)[1]) if "." in raw else 0
        if len(raw) > 40 or precision > 8:
            bad.append(match.group(0))
            continue
        try:
            amount, factor = Decimal(raw), _FACTORS[match.group(2) or ""]
            with localcontext() as context:
                context.prec = 100
                if not any((value / factor).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP) == amount for value in allowed):
                    bad.append(match.group(0))
        except (InvalidOperation, ValueError):
            bad.append(match.group(0))
    return bad


def explicitly_distinguishes_periods(text: str) -> bool:
    """Recognize limited direct period-comparison language, not general semantics."""
    if not isinstance(text, str) or len(text) > 3000:
        return False
    for sentence in re.split(r"[。！？!?;；\n]", text):
        if not re.search(r"期间|财年|年度|\b(?:periods?|fiscal|years?)\b", sentence, re.IGNORECASE):
            continue
        if re.search(r"并非\s*不同|不是\s*不同|未必\s*不同|\bnot\s+different\b", sentence, re.IGNORECASE):
            continue
        if re.search(r"不同|不一致|不一样|不相同|不(?:能|会|应|是|属于|覆盖)?(?:完全)?(?:相同|一致|同一)|"
                     r"\bdifferent\b|\bnot\s+(?:the\s+)?same\b", sentence, re.IGNORECASE):
            return True
    return False
