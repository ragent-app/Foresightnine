"""Conservative, auditable cross-source numeric comparison."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from pydantic import ValidationError

from .research_calculations import NumericFact, _is_ratio_scale_factor, quotation_is_inside_table_row
from .research_analysis import _directional_negative_amount, _directional_negative_percent, _quoted_numbers
from .research_sources import ResearchSource, partial_ascii_token


_PERCENT = re.compile(r"(?<![A-Za-z0-9_./-])([-−]?(?:(?:\d+-)?\d+/\d+|\d+(?:\.\d+)?))\s*(?:%|％|percent\b)", re.IGNORECASE)
_STRICT_UPPER_BOUND = re.compile(r"(?:\bless\s+than|\bbelow|\bunder|<|小于|小於|低于|低於|少于|少於)\s*$", re.IGNORECASE)
_DECIMAL_TOKEN = re.compile(r"(?<![\w.])[-−]?\d+(?:\.\d+)?(?![\w.])")
_RELATED_DECIMAL = re.compile(r"(?<![\w.])[-−]?\d+(?:\.\d+)?(?!\w|\.\d)")


def _strict_upper_percentages(text: str) -> list[tuple[Decimal, str]]:
    """Extract only explicitly strict upper percentage bounds, not exact values."""
    result = []
    for match in _PERCENT.finditer(text):
        prefix = text[max(0, match.start() - 32):match.start()]
        bound = _STRICT_UPPER_BOUND.search(prefix)
        if not bound or re.search(r"\b(?:not|never|no)\b|[不非未]", prefix[max(0, bound.start() - 20):bound.start()], re.IGNORECASE):
            continue
        values = _quoted_numbers(match.group(1).replace("−", "-"))
        if len(values) == 1:
            result.append((next(iter(values)), match.group(0)))
    return result


def _only_strict_upper_percentage(text: str, value: Decimal) -> bool:
    """Avoid equating a sole decimal upper bound with an unrelated exact fact.

    Any fractions, multiple occurrences, or ambiguous spellings keep the
    existing conservative fact-candidate behavior.
    """
    if re.search(r"\d\s*/\s*\d", text):
        return False
    occurrences = []
    for match in _DECIMAL_TOKEN.finditer(text):
        try:
            if Decimal(match.group(0).replace("−", "-")) == value:
                occurrences.append(match)
        except InvalidOperation:
            continue
    return (len(occurrences) == 1 and
            any(v == value for v, _ in _strict_upper_percentages(text)))
_ZH_USD_AMOUNT = re.compile(r"(?<![\d,.])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:个\s*)?(万亿|千亿|百亿|十亿|千万|百万|十万|亿|万|千|百)?\s*(美元|美金)")
_ZH_USD_TO_MILLIONS = {"万亿": Decimal("1000000"), "千亿": Decimal("100000"), "百亿": Decimal("10000"),
                       "十亿": Decimal("1000"), "亿": Decimal("100"), "千万": Decimal("10"),
                       "百万": Decimal("1"), "十万": Decimal("0.1"), "万": Decimal("0.01"),
                       "千": Decimal("0.001"), "百": Decimal("0.0001"), "": Decimal("0.000001")}
_ZH_PUBLICATION = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月(?:\s*(3[01]|[12]\d|0?[1-9])\s*日)?\s*(?:发布|公布|发表|刊登)")
_EN_PUBLICATION = re.compile(r"\b(?:published|released|issued)\s+(?:on|in)\s+((?:19|20)\d{2})-(0[1-9]|1[0-2])(?:-(0[1-9]|[12]\d|3[01]))?(?![-\d])", re.IGNORECASE)
_RATE_NUMBER = r"\d+(?:-\d+/\d+|/\d+|\.\d+)?"
_BASIS_POINTS = re.compile(r"(?<![A-Za-z0-9_./-])([-−]?\d+(?:\.\d+)?)\s*(?:[个個]\s*)?(?:基[点點]|basis\s+points?\b|bps\b)", re.IGNORECASE)
_POINTS = re.compile(r"(?<![A-Za-z0-9_./-])([-−]?(?:(?:\d+-)?\d+/\d+|\d+(?:\.\d+)?))\s*(?:[个個]?百分[点點]|percentage\s+points?\b)", re.IGNORECASE)
_RATE_LEVEL_POINTS = re.compile(
    rf"(?:利率目[标標][区區][间間]|目[标標]利率[区區][间間]|[联聯]邦基金利率目[标標][区區][间間]|利率[区區][间間]|目[标標][区區][间間]|"
    rf"(?:interest\s+rate|federal\s+funds\s+rate|target)\s+(?:target\s+)?range)"
    rf"(?P<between>[^。！？.!?;；\n]{{0,48}}?)(?P<range>{_RATE_NUMBER}\s*(?:至|到|to|–|—)\s*{_RATE_NUMBER})\s*(?:[个個]?百分[点點]|percentage\s+points?)",
    re.IGNORECASE)
_RATE_CHANGE = re.compile(r"上[调調]|下[调調]|上升|下降|扩大|缩小|增加|減少|减少|升高|降低|[调調]整|"
                          r"\b(?:adjust|increase|decrease|narrow|widen|change|cut|rise|raise|lower)\w*\b", re.IGNORECASE)


_METRIC_SUBJECTS = {
    "GDP": re.compile(r"\b(?:GDP|gross\s+domestic\s+product)\b|国内生产总值", re.IGNORECASE),
    "GDI": re.compile(r"\b(?:GDI|gross\s+domestic\s+income)\b|国内生产总收入", re.IGNORECASE),
    "personal saving rate": re.compile(r"\b(?:personal\s+)?savings?\s+rate\b|(?:个人)?储蓄率", re.IGNORECASE),
    "PCE price index": re.compile(r"\b(?:PCE|personal\s+consumption\s+expenditures)\s+price\s+index\b|个人消费支出物价指数", re.IGNORECASE),
}
_SENTENCE_END = re.compile(r"[.!?。！？](?=\s|$)")


def _table_header_context(source: ResearchSource, start: int, quotation: str,
                          citations: list[dict]) -> tuple[int, int] | None:
    """Find a nearby fiscal table header omitted from a cited numeric row.

    This is a bounded cue, not validation of column alignment or table units.
    Only consecutive extracted table lines are considered part of the table.
    """
    if (not quotation.lstrip().startswith("|") and
            not quotation_is_inside_table_row(source.text, start, quotation)) or not re.search(r"\d", quotation):
        return None
    if re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", quotation):
        return None
    left = max(0, start - 1200)
    before = source.text[left:start]
    headers = list(re.finditer(r"(?im)^[ \t]*\|[^\n]*\byears?\s+ended\b[^\n]*", before))
    if not headers:
        return None
    header = headers[-1]
    tail = before[header.end():]
    if any(line.strip() and not line.lstrip().startswith("|") for line in tail.splitlines()):
        return None
    # Extractors often place the year labels on the next table line rather
    # than on the "Year Ended" line. Only inspect a few adjacent rows.
    header_end_in_before = header.end()
    years = set(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", header.group()))
    if len(years) < 2:
        for row in list(re.finditer(r"(?m)^[ \t]*\|[^\n]*", tail))[:4]:
            years.update(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", row.group()))
            if len(years) >= 2:
                header_end_in_before = header.end() + row.end()
                break
    if len(years) < 2:
        return None
    header_start, header_end = left + header.start(), left + header_end_in_before
    unit_before_start = max(0, header_start - 250)
    unit_before = source.text[unit_before_start:header_start]
    units_before = list(re.finditer(r"\b(?:millions?|billions?|thousands?)\b", unit_before, re.IGNORECASE))
    unit_after = source.text[header_end:min(start, header_end + 250)]
    units_after = list(re.finditer(r"\b(?:millions?|billions?|thousands?)\b", unit_after, re.IGNORECASE))
    if units_after:
        unit_start = header_end + units_after[0].start()
    elif units_before:
        unit_start = unit_before_start + units_before[-1].start()
    else:
        unit_start = header_start
    has_unit = bool(units_after or units_before)
    def covered(a: int, b: int) -> bool:
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("source_id") != source.source_id or citation.get("partial_token"):
                continue
            loc, quote = citation.get("location"), citation.get("quotation")
            if not isinstance(loc, dict) or loc.get("status") == "ambiguous" or not isinstance(quote, str):
                continue
            x, y = loc.get("character_start"), loc.get("character_end")
            if type(x) is int and type(y) is int and 0 <= x <= a < b <= y <= len(source.text) and source.text[x:y] == quote:
                return True
        return False
    unit_match = units_after[0] if units_after else units_before[-1] if units_before else None
    if covered(header_start, header_end) and (unit_match is None or covered(unit_start, unit_start + len(unit_match.group()))):
        return None
    # Include header and unit whether the unit occurs before or after years.
    context_start = min(header_start, max(left, unit_start - 100)) if has_unit else header_start
    context_end = max(header_end, unit_start + len(unit_match.group())) if unit_match else header_end
    return context_start, context_end


def _unfinished_quote_context(source: ResearchSource, end: int, quotation: str) -> int | None:
    """Identify a narrow dangling conjunction/phrase as a review cue."""
    if not re.search(r"\b(?:and|or)(?:\s+[A-Za-z]+(?:[-‑][A-Za-z]+)?)?\s*$", quotation, re.IGNORECASE):
        return None
    tail = source.text[end:min(len(source.text), end + 200)]
    if not re.match(r"\s+[A-Za-z]", tail) or "\f" in tail[:20]:
        return None
    boundary = re.search(r"[.!?](?=\s|$)|\f", tail)
    if boundary:
        return end + (boundary.start() if boundary.group() == "\f" else boundary.end())
    # Do not cut the required continuation in the middle of a word.
    last_space = max(tail.rfind(" "), tail.rfind("\n"))
    return end + last_space if last_space > 0 else None


_RELATED_PHRASE = re.compile(r"(?=\b([A-Za-z]{4,})\s+([A-Za-z]{4,})\b)")
_RELATED_COMMON = {"about", "after", "before", "between", "committee", "could", "during", "federal",
                   "following", "from", "have", "into", "more", "only", "other", "policy", "reported",
                   "said", "source", "statement", "than", "that", "their", "these", "they", "this",
                   "those", "under", "using", "were", "when", "which", "will", "with", "would"}


def _following_related_sentence(source: ResearchSource, end: int, claim: str, citations: list[dict]) -> tuple[int, int] | None:
    """Find a nearby unquoted sentence sharing a distinctive exact phrase.

    This is a source-navigation cue only, not a claim-support judgment.
    """
    if len(claim) > 3000 or end >= len(source.text):
        return None
    tail = source.text[end:min(len(source.text), end + 600)]
    leading = len(tail) - len(tail.lstrip())
    if leading > 80 or "\f" in tail[:leading] or leading >= len(tail):
        return None
    body = tail[leading:]
    boundary = _SENTENCE_END.search(body)
    if boundary is None or boundary.end() > 450 or "\f" in body[:boundary.end()]:
        return None
    left, right = end + leading, end + leading + boundary.end()
    sentence = source.text[left:right]
    # Only exact, authorized, source-aligned quotations can clear a cue.
    quoted = []
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("source_id") != source.source_id or citation.get("partial_token"):
            continue
        loc, quote = citation.get("location"), citation.get("quotation")
        if not isinstance(loc, dict) or loc.get("status") == "ambiguous" or not isinstance(quote, str):
            continue
        a, b = loc.get("character_start"), loc.get("character_end")
        if (type(a) is int and type(b) is int and 0 <= a < b <= len(source.text) and
                source.text[a:b] == quote and not partial_ascii_token(source.text, a, b)):
            quoted.append(quote)
    seen = set()
    matched = 0
    for match in _RELATED_PHRASE.finditer(claim):
        a, b = match.group(1).casefold(), match.group(2).casefold()
        pair = (a, b)
        if pair in seen or a in _RELATED_COMMON or b in _RELATED_COMMON or len(a) + len(b) < 11:
            continue
        seen.add(pair)
        pattern = re.compile(r"(?<![A-Za-z])" + re.escape(a) + r"\s+" + re.escape(b) + r"(?![A-Za-z])", re.IGNORECASE)
        if pattern.search(sentence) and not any(pattern.search(q) for q in quoted):
            matched += 1
            if matched >= 2:
                return left, right
    return None


def flag_citation_subject_context(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Flag selected quotations that omit a recognizable surrounding subject.

    This is a review cue, not a verdict about any claim. Only exact, unique,
    display-authorized source positions are inspected. Ambiguous sentences or
    unrecognized measures are deliberately not classified.
    """
    source_map = {source.source_id: source for source in sources if source.permissions.display and not source.is_simulated}
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("citation_context_warnings", None)
        citations = entry.get("citations")
        if not isinstance(citations, list):
            output.append(entry)
            continue
        warnings = []
        for index, citation in enumerate(citations[:10]):
            if not isinstance(citation, dict) or citation.get("partial_token"):
                continue
            source_id = citation.get("source_id")
            source = source_map.get(source_id) if isinstance(source_id, str) else None
            location, quote = citation.get("location"), citation.get("quotation")
            if (source is None or not isinstance(location, dict) or location.get("status") == "ambiguous" or
                    not isinstance(quote, str) or not quote):
                continue
            start, end = location.get("character_start"), location.get("character_end")
            if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source.text) or
                    source.text[start:end] != quote or end - start > 1000):
                continue
            table_context = _table_header_context(source, start, quote, citations)
            if table_context:
                warnings.append({"source_id": source_id, "citation_index": index, "context_metric": "financial table header",
                                 "context_character_start": table_context[0], "context_character_end": table_context[1],
                                 "status": "requires_review"})
                if len(warnings) >= 5:
                    break
                continue
            unfinished_end = _unfinished_quote_context(source, end, quote)
            if unfinished_end is not None and unfinished_end > end:
                warnings.append({"source_id": source_id, "citation_index": index, "context_metric": "unfinished quoted phrase",
                                 "context_character_start": start, "context_character_end": unfinished_end,
                                 "status": "requires_review"})
                if len(warnings) >= 5:
                    break
                continue
            claim_text = entry.get("text")
            related = (_following_related_sentence(source, end, claim_text, citations)
                       if isinstance(claim_text, str) else None)
            if related is not None:
                warnings.append({"source_id": source_id, "citation_index": index,
                                 "context_metric": "related unquoted sentence",
                                 "context_character_start": related[0], "context_character_end": related[1],
                                 "status": "requires_review"})
                if len(warnings) >= 5:
                    break
            # A pronoun or leading amount can omit the subject and period in
            # the preceding part of the same sentence. This is a context cue,
            # never a conclusion that the citation supports a claim.
            pronoun_start = bool(re.match(r"(?i)^\s*(?:it|they|he|she|its|their)\b", quote))
            amount_start = bool(re.match(r"(?i)^\s*(?:US\$|[$€£¥])\s*[-−]?\d", quote))
            if pronoun_start or amount_start:
                prefix_start = max(0, start - 250)
                prefix = source.text[prefix_start:start]
                boundaries = list(_SENTENCE_END.finditer(prefix))
                left = prefix_start + (boundaries[-1].end() if boundaries else 0)
                antecedent = source.text[left:start]
                covered = False
                required_left = left + len(antecedent) - len(antecedent.lstrip())
                required_right = left + len(antecedent.rstrip())
                for other in citations:
                    if not isinstance(other, dict) or other.get("source_id") != source_id or other.get("partial_token"):
                        continue
                    loc, q = other.get("location"), other.get("quotation")
                    if not isinstance(loc, dict) or loc.get("status") == "ambiguous" or not isinstance(q, str):
                        continue
                    a, b = loc.get("character_start"), loc.get("character_end")
                    if (type(a) is int and type(b) is int and 0 <= a <= required_left < required_right <= b <= len(source.text)
                            and source.text[a:b] == q and not partial_ascii_token(source.text, a, b)):
                        covered = True
                        break
                if (not covered and "\f" not in antecedent and len(antecedent) < 250 and
                        len(re.findall(r"[A-Za-z]{3,}", antecedent)) >= 2 and
                        antecedent[-1:].isspace()):
                    warnings.append({"source_id": source_id, "citation_index": index,
                                     "context_metric": "personal-pronoun antecedent" if pronoun_start else "numeric quotation subject",
                                     "context_character_start": left, "context_character_end": start,
                                     "status": "requires_review"})
                    if len(warnings) >= 5:
                        break
                    continue
            # A relative clause can contain another metric (e.g. GDP) while
            # still omitting the subject it describes (e.g. imports). Check
            # this before the metric shortcut so a later noun cannot hide the
            # missing antecedent. This exposes context, not an entailment verdict.
            if re.match(r"(?i)^\s*(?:which|who|that)\s+(?:is|are|was|were|has|have|had)\b", quote):
                prefix_start = max(0, start - 200)
                prefix = source.text[prefix_start:start]
                boundaries = list(_SENTENCE_END.finditer(prefix))
                left = prefix_start + (boundaries[-1].end() if boundaries else 0)
                antecedent = source.text[left:start]
                if (antecedent.strip() and "\f" not in antecedent and len(antecedent) < 200 and
                        re.search(r"[\w)]\s*,\s*$", antecedent)):
                    warnings.append({"source_id": source_id, "citation_index": index,
                                     "context_metric": "relative-clause antecedent",
                                     "context_character_start": left, "context_character_end": start,
                                     "status": "requires_review"})
                    if len(warnings) >= 5:
                        break
                    continue
            # The metric may be present while the entity owning it is cut
            # off, e.g. "River's revenue" quoted from "revenue" onward.
            # Preserve that possessive only as context, not as quoted proof.
            if re.match(r"(?i)^\s*(?:revenue|sales|income|profit|earnings|assets|liabilities|cash|debt)\b", quote):
                prefix_start = max(0, start - 160)
                prefix = source.text[prefix_start:start]
                prior_boundaries = list(_SENTENCE_END.finditer(prefix))
                if prior_boundaries:
                    prefix_start += prior_boundaries[-1].end()
                    prefix = source.text[prefix_start:start]
                entity = re.search(r"(?<![\w])([A-Z][A-Za-z0-9&.'-]*(?:\s+[A-Z][A-Za-z0-9&.'-]*){0,4}['’]s)\s+$", prefix)
                if entity and "\f" not in prefix[entity.start():]:
                    entity_start = prefix_start + entity.start(1)
                    entity_end = prefix_start + entity.end(1)
                    covered = any(
                        isinstance(other, dict) and other.get("source_id") == source_id and not other.get("partial_token") and
                        isinstance(other.get("location"), dict) and other["location"].get("status") != "ambiguous" and
                        type(other["location"].get("character_start")) is int and type(other["location"].get("character_end")) is int and
                        0 <= other["location"]["character_start"] <= entity_start < entity_end <= other["location"]["character_end"] <= len(source.text) and
                        isinstance(other.get("quotation"), str) and
                        source.text[other["location"]["character_start"]:other["location"]["character_end"]] == other["quotation"] and
                        not partial_ascii_token(source.text, other["location"]["character_start"], other["location"]["character_end"])
                        for other in citations)
                    if not covered:
                        warnings.append({"source_id": source_id, "citation_index": index, "context_metric": "possessive entity",
                                         "context_character_start": entity_start, "context_character_end": start,
                                         "status": "requires_review"})
                        if len(warnings) >= 5:
                            break
                        continue
            if any(pattern.search(quote) for pattern in _METRIC_SUBJECTS.values()):
                continue
            before = source.text[max(0, start - 400):start]
            sentence_ends = list(_SENTENCE_END.finditer(before))
            left = max(0, start - 400) + (sentence_ends[-1].end() if sentence_ends else 0)
            before = source.text[left:start]
            # A page break or distant sentence start is insufficient context.
            if "\f" in before or len(before) >= 400:
                continue
            # A quotation may begin in the middle of a multiword subject, for
            # example "index increased" after "PCE price". Inspect the
            # boundary as well as subjects wholly preceding the quotation.
            # Keep the sentence prefix as context so qualifiers such as
            # "excluding food and energy" are not silently dropped.
            boundary = len(before)
            subject_text = before + quote
            subject_matches = [(name, match) for name, pattern in _METRIC_SUBJECTS.items()
                               if (match := pattern.search(subject_text)) is not None and match.start() < boundary]
            if len(subject_matches) != 1:
                continue
            name, match = subject_matches[0]
            warnings.append({"source_id": source.source_id, "citation_index": index,
                             "context_metric": name, "context_character_start": left,
                             "context_character_end": left + match.end(), "status": "requires_review"})
            if len(warnings) >= 5:
                break
        if warnings:
            entry["citation_context_warnings"] = warnings
        output.append(entry)
    return output


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
                        if (not _RATE_CHANGE.search(match.group("between")) or
                            re.search(r"(?:至|到|\bto)\s*$", match.group("between"), re.IGNORECASE))][:5]
            if warnings:
                entry["rate_unit_warnings"] = warnings
        output.append(entry)
    return output


_EN_EXPLICIT_USD = re.compile(r"(?<![A-Za-z0-9_])(?:USD|US\$)\s*([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(trillion|billion|million|thousand)?\b|(?<![A-Za-z0-9_])([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(trillion|billion|million|thousand)?\s*(?:USD|U\.S\. dollars?)\b", re.IGNORECASE)
_USD_SCALE = {"": Decimal("0.000001"), "thousand": Decimal("0.001"), "million": Decimal("1"), "billion": Decimal("1000"), "trillion": Decimal("1000000")}


def _explicit_usd_amounts(text: str) -> list[tuple[str, Decimal]]:
    amounts = []
    for match in _ZH_USD_AMOUNT.finditer(text):
        raw = match.group(1).replace(",", "").replace("−", "-")
        if len(raw) <= 40:
            with localcontext() as context:
                context.prec = 100
                amounts.append((match.group(0), Decimal(raw) * _ZH_USD_TO_MILLIONS[match.group(2) or ""]))
    for match in _EN_EXPLICIT_USD.finditer(text):
        raw = (match.group(1) or match.group(3)).replace(",", "").replace("−", "-")
        if len(raw) <= 40:
            with localcontext() as context:
                context.prec = 100
                amounts.append((match.group(0), Decimal(raw) * _USD_SCALE[(match.group(2) or match.group(4) or "").lower()]))
    return amounts



_DOLLAR_AMOUNT = re.compile(
    r"(?<![A-Za-z0-9_$])([-−]?)(?:US)?\$\s*([-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    r"\s*(trillion|billion|million|thousand|bn|mn)?\b", re.IGNORECASE)
_DOLLAR_SCALE = {**_USD_SCALE, "bn": Decimal("1000"), "mn": Decimal("1")}


def _dollar_amounts(text: str) -> list[tuple[str, Decimal]]:
    result = []
    for match in _DOLLAR_AMOUNT.finditer(text):
        sign, raw = match.group(1).replace("−", "-"), match.group(2).replace(",", "").replace("−", "-")
        if len(raw) > 40 or (sign and raw.startswith("-")):
            continue
        with localcontext() as context:
            context.prec = 100
            result.append((match.group(0), Decimal(sign + raw) * _DOLLAR_SCALE[(match.group(3) or "").casefold()]))
    return result


def _flag_uncited_expressions(claims: list[dict], sources: list[ResearchSource], key: str, parse) -> list[dict]:
    """Compare bounded expressions against located quotations, not entailment."""
    source_map = {s.source_id: s for s in sources if s.permissions.display and not s.is_simulated}
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop(key, None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        quoted = set()
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("partial_token"):
                continue
            source = source_map.get(citation.get("source_id")) if isinstance(citation.get("source_id"), str) else None
            quote, location = citation.get("quotation"), citation.get("location")
            if source is None or not isinstance(quote, str) or not isinstance(location, dict) or location.get("status") == "ambiguous":
                continue
            start, end = location.get("character_start"), location.get("character_end")
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(source.text)
                    and source.text[start:end] == quote and not partial_ascii_token(source.text, start, end)):
                quoted.update(value for _, value in parse(quote))
        missing = []
        for raw, value in parse(text):
            if value not in quoted and raw not in missing:
                missing.append(raw)
                if len(missing) == 5:
                    break
        if missing:
            entry[key] = missing
        output.append(entry)
    return output



def flag_uncited_usd_amounts(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Flag explicit USD amounts absent from located quotations, not semantic support."""
    return _flag_uncited_expressions(claims, sources, "uncited_claim_amounts", _explicit_usd_amounts)


def flag_uncited_dollar_amounts(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Compare bare dollar expressions without deciding their currency or meaning."""
    return _flag_uncited_expressions(claims, sources, "uncited_dollar_amounts", _dollar_amounts)


_QUARTER_YEAR_FIRST = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[:/\-]\s*[Qq]([1-4])(?!\d)")
_QUARTER_Q_FIRST = re.compile(r"(?i)\bQ([1-4])\s*[- /]?\s*((?:19|20)\d{2})(?!\d)")
_QUARTER_ENGLISH = re.compile(r"(?i)\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?((?:19|20)\d{2})(?!\d)")
_QUARTER_CHINESE = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*第?\s*([一二三四1-4])\s*季(?:度)?")
_QUARTER_VALUES = {"first":1,"1st":1,"second":2,"2nd":2,"third":3,"3rd":3,"fourth":4,"4th":4,"一":1,"二":2,"三":3,"四":4}


def _explicit_quarters(text: str) -> list[tuple[str, tuple[int, int]]]:
    matches = []
    for pattern in (_QUARTER_YEAR_FIRST, _QUARTER_Q_FIRST, _QUARTER_ENGLISH, _QUARTER_CHINESE):
        for match in pattern.finditer(text):
            if any(match.start() < existing_end and existing_start < match.end()
                   for existing_start, existing_end, _, _ in matches):
                continue
            a,b=match.group(1).casefold(),match.group(2).casefold()
            year, quarter = (a,b) if len(a)==4 and a.isdigit() else (b,a)
            number = int(quarter) if quarter.isdigit() else _QUARTER_VALUES[quarter]
            matches.append((match.start(),match.end(),match.group(0),(int(year),number)))
    return [(raw,value) for _,_,raw,value in sorted(matches)]


def flag_uncited_quarters(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Flag explicit quarter/year labels absent from located quotations, not support."""
    return _flag_uncited_expressions(claims, sources, "uncited_quarters", _explicit_quarters)


_BOTH_NARROW_DEFICIT = re.compile(
    r"\bboth(?:\s+of\s+which|\s+changes?)?\s+(?:(?:would|will|should|can|could)\s+)?"
    r"(?:narrow|reduce|shrink|decrease)\s+(?:the\s+)?(?:overall\s+|total\s+)?deficit\b"
    r"|(?:两者|兩者|二者|两项|兩項)(?:变化|變化|变动|變動)?(?:均|都|皆|都会|都會|均会|均會)?"
    r"[^。！？；;]{0,14}?(?:(?:缩小|縮小|收窄|减少|減少)[^。！？；;]{0,8}?(?:总|總|总体|總體|整体|整體)?(?:贸易|貿易)?逆差"
    r"|(?:总|總|总体|總體|整体|整體)?(?:贸易|貿易)?逆差[^。！？；;]{0,8}?(?:缩小|縮小|收窄|减少|減少))", re.IGNORECASE)
_GOODS_DEFICIT_FALL = re.compile(r"\b(?:decrease|decline|fall|reduction)\s+(?:in\s+)?(?:the\s+)?goods\s+deficit\b|\bgoods\s+deficit\s+(?:fell|decreased|declined|narrowed)\b|(?:商品|货物|貨物)逆差(?:下降|减少|減少|收窄)", re.IGNORECASE)
_SERVICES_SURPLUS_FALL = re.compile(r"\b(?:decrease|decline|fall|reduction)\s+(?:in\s+)?(?:the\s+)?services\s+surplus\b|\bservices\s+surplus\s+(?:fell|decreased|declined|shrank)\b|(?:服务|服務)顺差(?:下降|减少|減少|收窄)", re.IGNORECASE)
_BOTH_RAISE_PROFIT = re.compile(
    r"\bboth(?:\s+of\s+which|\s+changes?)?\s+(?:(?:would|will|should|can|could)\s+)?"
    r"(?:increase|raise|improve|boost)\s+(?:the\s+)?operating\s+(?:profit|income)\b"
    r"|(?:两者|兩者|二者|两项|兩項)(?:变化|變化|变动|變動)?(?:均|都|皆|都会|都會|均会|均會)?"
    r"[^。！？；;]{0,14}?(?:(?:提高|提升|增加|推动|推動)[^。！？；;]{0,8}?(?:营业|營業|经营|經營)(?:利润|利潤)"
    r"|(?:营业|營業|经营|經營)(?:利润|利潤)[^。！？；;]{0,8}?(?:上升|增长|增長|增加|提高))", re.IGNORECASE)
_REVENUE_FALL = re.compile(r"\b(?:revenues?|sales)\s+(?:fell|decreased|declined|dropped)\b|\b(?:decrease|decline|fall|reduction)\s+in\s+(?:the\s+)?(?:revenues?|sales)\b|(?:营收|營收|收入)(?:下降|减少|減少|下滑)", re.IGNORECASE)
_OPERATING_EXPENSE_FALL = re.compile(r"\boperating\s+expenses?\s+(?:fell|decreased|declined|dropped)\b|\b(?:decrease|decline|fall|reduction)\s+in\s+(?:the\s+)?operating\s+expenses?\b|(?:营业|營業|经营|經營)费用(?:下降|减少|減少|下滑)|(?:營業|經營)費用(?:下降|減少|下滑)", re.IGNORECASE)
_COMPONENT_NEGATION = re.compile(r"\b(?:not|neither|incorrect|false|deny|denies)\b|并非|並非|并不|並不|不认为|不認為|不是|没有|沒有|不能|未必|无法|無法|并未|並未", re.IGNORECASE)
_REVISION_DIRECTION = re.compile(r"\b(?:downward|upward)\s+revision\b|\brevis(?:ed|ion)\s+(?:up|down|upwards?|downwards?)\b|(?:向下|向上|下调|下調|上调|上調)(?:修订|修訂|修正|调整|調整)|(?:修订|修訂|修正)(?:向下|向上|下调|下調|上调|上調)", re.IGNORECASE)
_REVISION_CONTRADICTION = re.compile(r"\b(?:contrary\s+to|contradict(?:s|ed)?|refut(?:e|es|ed))\b|(?:反驳|反駁|与.{0,12}相矛盾|與.{0,12}相矛盾)", re.IGNORECASE)


def flag_component_direction(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Flag narrow direction risks, including revision versus change.

    A warning is a review cue, not proof of claim support or accounting basis.
    Both component movements must occur in the same located quotation for the
    accounting rules. A revision warning is a cue, not a determination that
    the claim is false: further evidence may establish a contradiction.
    """
    source_map = {s.source_id:s for s in sources if s.permissions.display and not s.is_simulated}
    rules = (("trade_deficit", _BOTH_NARROW_DEFICIT, _GOODS_DEFICIT_FALL, _SERVICES_SURPLUS_FALL),
             ("operating_profit", _BOTH_RAISE_PROFIT, _REVENUE_FALL, _OPERATING_EXPENSE_FALL))
    output=[]
    for claim in claims:
        entry=deepcopy(claim)
        entry.pop("balance_direction_warnings",None)
        text,citations=entry.get("text"),entry.get("citations")
        if not isinstance(text,str) or len(text)>3000 or not isinstance(citations,list):
            output.append(entry);continue
        revision = _REVISION_DIRECTION.search(text)
        contrary = _REVISION_CONTRADICTION.search(text)
        if revision and contrary and not _COMPONENT_NEGATION.search(text[max(0,contrary.start()-24):contrary.start()]):
            for index, citation in enumerate(citations[:10]):
                if not isinstance(citation,dict) or citation.get("partial_token"):
                    continue
                source=source_map.get(citation.get("source_id")) if isinstance(citation.get("source_id"),str) else None
                quote,loc=citation.get("quotation"),citation.get("location")
                if source is None or not isinstance(quote,str) or not isinstance(loc,dict) or loc.get("status")=="ambiguous":
                    continue
                a,b=loc.get("character_start"),loc.get("character_end")
                if (type(a) is int and type(b) is int and 0<=a<b<=len(source.text) and source.text[a:b]==quote
                        and not partial_ascii_token(source.text,a,b) and _REVISION_DIRECTION.search(quote)):
                    entry.setdefault("balance_direction_warnings",[]).append({"source_id":source.source_id,"citation_index":index,
                        "basis":"revision_vs_change","claimed_text":contrary.group(0),"status":"requires_review"})
                    break
        for basis,claim_pattern,component_one,component_two in rules:
            matches=list(claim_pattern.finditer(text))[:5]
            matches=[m for m in matches if not _COMPONENT_NEGATION.search(text[max(0,m.start()-50):m.start()])]
            if not matches:
                continue
            for index,citation in enumerate(citations[:10]):
                if not isinstance(citation,dict) or citation.get("partial_token"):
                    continue
                source=source_map.get(citation.get("source_id")) if isinstance(citation.get("source_id"),str) else None
                quote,loc=citation.get("quotation"),citation.get("location")
                if source is None or not isinstance(quote,str) or not isinstance(loc,dict) or loc.get("status")=="ambiguous":
                    continue
                a,b=loc.get("character_start"),loc.get("character_end")
                if (type(a) is int and type(b) is int and 0<=a<b<=len(source.text) and source.text[a:b]==quote
                        and not partial_ascii_token(source.text,a,b) and component_one.search(quote)
                        and component_two.search(quote)):
                    entry.setdefault("balance_direction_warnings",[]).append({"source_id":source.source_id,"citation_index":index,
                        "basis":basis,"claimed_text":matches[0].group(0),"status":"requires_review"})
                    break
        output.append(entry)
    return output


def flag_uncited_percentage_points(claims: list[dict]) -> list[dict]:
    """Flag percentage-point amounts absent from quotations, not entailment."""
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("uncited_percentage_points", None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        quoted: set[Decimal] = set()
        for citation in citations:
            quote = citation.get("quotation") if isinstance(citation, dict) else None
            if not isinstance(quote, str):
                continue
            for match in _POINTS.finditer(quote):
                if len(match.group(1)) <= 20:
                    values = _quoted_numbers(match.group(1).replace("−", "-"))
                    if len(values) == 1:
                        quoted.add(next(iter(values)))
            for match in _BASIS_POINTS.finditer(quote):
                if len(match.group(1)) <= 20:
                    quoted.add(Decimal(match.group(1).replace("−", "-")) / 100)
        missing = []
        for match in _POINTS.finditer(text):
            values = _quoted_numbers(match.group(1).replace("−", "-")) if len(match.group(1)) <= 20 else set()
            if (len(values) != 1 or next(iter(values)) not in quoted) and match.group(0) not in missing:
                missing.append(match.group(0))
                if len(missing) == 5:
                    break
        if missing:
            entry["uncited_percentage_points"] = missing
        output.append(entry)
    return output


def flag_uncited_basis_points(claims: list[dict]) -> list[dict]:
    """Flag basis-point figures missing from quotations, not semantic support."""
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("uncited_basis_points", None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        quoted: set[Decimal] = set()
        for citation in citations:
            quote = citation.get("quotation") if isinstance(citation, dict) else None
            if not isinstance(quote, str):
                continue
            for match in _BASIS_POINTS.finditer(quote):
                if len(match.group(1)) <= 20:
                    quoted.add(Decimal(match.group(1).replace("−", "-")))
            for match in _POINTS.finditer(quote):
                if len(match.group(1)) > 20:
                    continue
                values = _quoted_numbers(match.group(1).replace("−", "-"))
                if len(values) == 1:
                    quoted.add(next(iter(values)) * 100)
        missing = []
        for match in _BASIS_POINTS.finditer(text):
            value = Decimal(match.group(1).replace("−", "-")) if len(match.group(1)) <= 20 else None
            if (value is None or value not in quoted) and match.group(0) not in missing:
                missing.append(match.group(0))
            if len(missing) == 5:
                break
        if missing:
            entry["uncited_basis_points"] = missing
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
    """Flag narrow Chinese USD unit mismatches against cited numeric facts.

    This is a review candidate, not a finding that a claim is false. It covers
    explicit Chinese USD units only, preserves rounding, and fails closed when
    model facts or citations are missing or ambiguous.
    """
    usable: list[tuple[str, str, Decimal, Decimal]] = []
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
                not re.search(r"\b(?:million|billion)s?\b", unit) or
                not ("usd" in unit or "dollar" in unit or currency == "USD") or currency not in {"", "USD"}):
            continue
        try:
            value = Decimal(str(item.get("value")))
            if not value.is_finite() or value not in _quoted_numbers(quote):
                continue
        except (InvalidOperation, ValueError):
            continue
        scales = set(re.findall(r"\b(million|billion)s?\b", unit))
        if len(scales) != 1:
            continue
        scale = Decimal(1000) if "billion" in scales else Decimal(1)
        with localcontext() as context:
            context.prec = 100
            usable.append((source_id, fact_id, value * scale, value))
    result: list[dict] = []
    for index, claim in enumerate(claims):
        text, citations = claim.get("text"), claim.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            continue
        cited = {c.get("source_id") for c in citations if isinstance(c, dict) and isinstance(c.get("source_id"), str)}
        candidates = [(fact_id, value, original) for source_id, fact_id, value, original in usable if source_id in cited]
        matches = list(_ZH_USD_AMOUNT.finditer(text))
        if not candidates or not matches or len(matches) > 20:
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
                                       for _, value, _ in candidates)
            except (InvalidOperation, ValueError):
                checked = []
                break
            checked.append((match, matches_fact))
        anchored = any(matched for _, matched in checked)
        # A single amount is comparable only as a conservative cue when its
        # bare number exactly matches the sole cited fact but its unit does not.
        single_unit_cue = (len(checked) == 1 and len(candidates) == 1 and
                           Decimal(checked[0][0].group(1).replace(",", "").replace("−", "-")) == candidates[0][2])
        if not checked or not (anchored or single_unit_cue):
            continue
        for match, matched in checked:
            if not matched:
                result.append({"claim_index": index, "claimed_amount": match.group(0),
                               "source_fact_ids": list(dict.fromkeys(fact_id for fact_id, _, _ in candidates))[:20],
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
        entry.pop("reused_percentages", None)
        entry.pop("uncited_percentage_bounds", None)
        text = entry.get("text")
        citations = entry.get("citations")
        if not isinstance(text, str) or not isinstance(citations, list):
            output.append(entry)
            continue
        # The same number can refer to different measures. A matching number
        # elsewhere in a quotation is not evidence for every occurrence.
        occurrences: dict[Decimal, list[str]] = defaultdict(list)
        for match in _PERCENT.finditer(text):
            if _is_ratio_scale_factor(text, match):
                continue
            values = _quoted_numbers(match.group(1).replace("−", "-"))
            if len(values) == 1:
                occurrences[next(iter(values))].append(match.group(0))
        reused = [matches[0] for matches in occurrences.values() if len(matches) > 1][:5]
        if reused:
            entry["reused_percentages"] = reused
        existing: set[Decimal] = set()
        for citation in citations:
            quote = citation.get("quotation") if isinstance(citation, dict) else None
            if isinstance(quote, str):
                existing.update(_quoted_numbers(quote))
        cited_bounds = {value for citation in citations if isinstance(citation, dict)
                        and isinstance(citation.get("quotation"), str)
                        for value, _ in _strict_upper_percentages(citation["quotation"])}
        missing_bounds = list(dict.fromkeys(raw for value, raw in _strict_upper_percentages(text)
                                           if value not in cited_bounds))[:5]
        if missing_bounds:
            entry["uncited_percentage_bounds"] = missing_bounds
        suggested = []
        uncited = []
        seen: set[Decimal] = set()
        for match in _PERCENT.finditer(text):
            if _is_ratio_scale_factor(text, match):
                continue
            values = _quoted_numbers(match.group(1).replace("−", "-"))
            if len(values) != 1:
                # An inexact or invalid fraction must remain visible for
                # review rather than disappearing from numeric checks.
                if match.group(0) not in uncited and len(uncited) < 5:
                    uncited.append(match.group(0))
                continue
            value = next(iter(values))
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


def suggest_usd_amount_evidence(claims: list[dict], facts: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Suggest unique, source-matched USD amount facts; never assert entailment.

    This deliberately handles only explicit Chinese USD amounts against
    uniquely located, unedited model facts in USD millions. Matching numbers
    does not establish entity, period, accounting basis, or claim support.
    """
    usable = []
    source_map = {source.source_id: source for source in sources if source.permissions.display and not source.is_simulated}
    id_counts = Counter(item.get("fact_id") for item in facts if isinstance(item.get("fact_id"), str))
    for item in facts:
        source_id, fact_id = item.get("source_id"), item.get("fact_id")
        quote, location = item.get("quotation"), item.get("location")
        unit = " ".join(str(item.get("unit") or "").casefold().split())
        currency = str(item.get("currency") or "").upper()
        if (item.get("verification") != "model_extraction_not_independently_verified" or item.get("authorship") == "user" or
                not isinstance(source_id, str) or not isinstance(fact_id, str) or id_counts[fact_id] != 1 or not isinstance(quote, str) or
                not quote or len(quote) > 1000 or not isinstance(location, dict) or location.get("status") == "ambiguous" or
                type(location.get("character_start")) is not int or type(location.get("character_end")) is not int or
                location["character_start"] < 0 or location["character_end"] - location["character_start"] != len(quote) or
                not re.fullmatch(r"(?:usd\s+)?millions?(?:\s+usd)?", unit) or
                not ("usd" in unit or currency == "USD") or currency not in {"", "USD"}):
            continue
        try:
            value = Decimal(str(item.get("value")))
            source = source_map.get(source_id)
            start, end = location["character_start"], location["character_end"]
            if (source is None or end > len(source.text) or source.text[start:end] != quote or
                    source.text.find(quote) != start or source.text.find(quote, start + 1) >= 0 or
                    partial_ascii_token(source.text, start, end) or not value.is_finite() or value not in _quoted_numbers(quote)):
                continue
        except (InvalidOperation, ValueError):
            continue
        usable.append((source_id, value, item))

    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("uncited_amounts", None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        cited_sources = {c.get("source_id") for c in citations if isinstance(c, dict) and isinstance(c.get("source_id"), str)}
        existing: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("partial_token"):
                continue
            source_id, quote, location = citation.get("source_id"), citation.get("quotation"), citation.get("location")
            source = source_map.get(source_id) if isinstance(source_id, str) else None
            if (source is None or not isinstance(quote, str) or not isinstance(location, dict) or
                    location.get("status") == "ambiguous"):
                continue
            start, end = location.get("character_start"), location.get("character_end")
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(source.text) and
                    source.text[start:end] == quote and not partial_ascii_token(source.text, start, end)):
                existing[source_id].append((start, end))
        previous_suggestions = entry.get("suggested_fact_evidence")
        suggestions = [item for item in previous_suggestions if isinstance(item, dict)] if isinstance(previous_suggestions, list) else []
        seen = {item.get("fact_id") for item in suggestions if isinstance(item, dict)}
        missing = []
        matches = list(_ZH_USD_AMOUNT.finditer(text))
        if len(matches) <= 20:
            for match in matches:
                raw = match.group(1).replace(",", "").replace("−", "-")
                places = len(raw.rsplit(".", 1)[1]) if "." in raw else 0
                if len(raw) > 40 or places > 8:
                    continue
                try:
                    amount, factor = Decimal(raw), _ZH_USD_TO_MILLIONS[match.group(2) or ""]
                    with localcontext() as context:
                        context.prec = 100
                        candidates = [(value, item) for source_id, value, item in usable if source_id in cited_sources and
                            (value / factor).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == amount]
                except (InvalidOperation, ValueError):
                    continue
                if len(candidates) != 1:
                    continue
                value, fact = candidates[0]
                fact_start, fact_end = fact["location"]["character_start"], fact["location"]["character_end"]
                if any(start <= fact_start and fact_end <= end for start, end in existing[fact["source_id"]]):
                    continue
                if match.group(0) not in missing and len(missing) < 5:
                    missing.append(match.group(0))
                if fact["fact_id"] not in seen and len(suggestions) < 5:
                    seen.add(fact["fact_id"])
                    suggestions.append({key: deepcopy(fact[key]) for key in
                        ("fact_id", "source_id", "value", "unit", "period", "quotation", "location") if key in fact} |
                        {"status": "requires_semantic_review"})
        if suggestions:
            entry["suggested_fact_evidence"] = suggestions
        if missing:
            entry["uncited_amounts"] = missing
        output.append(entry)
    return output


def suggest_uncited_numeric_facts(claims: list[dict], facts: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Suggest uniquely located fact passages for uncited numbers, without asserting support."""
    source_map = {s.source_id: s for s in sources if s.permissions.display and not s.is_simulated}
    ids = Counter(f.get("fact_id") for f in facts if isinstance(f.get("fact_id"), str))
    usable: list[tuple[str, Decimal, dict]] = []
    for fact in facts:
        sid, fid, quote, loc = fact.get("source_id"), fact.get("fact_id"), fact.get("quotation"), fact.get("location")
        source = source_map.get(sid) if isinstance(sid, str) else None
        if (source is None or not isinstance(fid, str) or ids[fid] != 1 or fact.get("authorship") == "user" or
                fact.get("verification") != "model_extraction_not_independently_verified" or
                not isinstance(quote, str) or not quote or len(quote) > 1000 or not isinstance(loc, dict) or
                loc.get("status") == "ambiguous"):
            continue
        a, b = loc.get("character_start"), loc.get("character_end")
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(source.text) or
                source.text[a:b] != quote or source.text.find(quote) != a or source.text.find(quote, a + 1) >= 0 or
                partial_ascii_token(source.text, a, b)):
            continue
        try:
            value = Decimal(str(fact.get("value")))
            direction_cue = (fact.get("direction_context_unverified") is True and
                             (_directional_negative_percent(value, str(fact.get("unit") or ""), quote) or
                              _directional_negative_amount(value, str(fact.get("unit") or ""), quote, fact.get("currency"))))
            if not value.is_finite() or value not in _quoted_numbers(quote) and not direction_cue:
                continue
        except (InvalidOperation, ValueError):
            continue
        usable.append((sid, value, fact))
    output = []
    for claim in claims:
        entry = deepcopy(claim)
        entry.pop("uncited_fact_values", None)
        text, citations = entry.get("text"), entry.get("citations")
        if not isinstance(text, str) or len(text) > 3000 or not isinstance(citations, list):
            output.append(entry)
            continue
        existing: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for citation in citations:
            if not isinstance(citation, dict) or citation.get("partial_token"):
                continue
            sid, quote, loc = citation.get("source_id"), citation.get("quotation"), citation.get("location")
            source = source_map.get(sid) if isinstance(sid, str) else None
            if source is None or not isinstance(quote, str) or not isinstance(loc, dict) or loc.get("status") == "ambiguous":
                continue
            a, b = loc.get("character_start"), loc.get("character_end")
            if (type(a) is int and type(b) is int and 0 <= a < b <= len(source.text) and
                    source.text[a:b] == quote and not partial_ascii_token(source.text, a, b)):
                existing[sid].append((a, b))
        numbers = _quoted_numbers(text)
        candidates = [(sid, value, fact) for sid, value, fact in usable if sid in existing and
                      (value in numbers or fact.get("direction_context_unverified") is True and
                       (_directional_negative_percent(value, str(fact.get("unit") or ""), text) or
                        _directional_negative_amount(value, str(fact.get("unit") or ""), text, fact.get("currency"))))
                      and not _only_strict_upper_percentage(text, value)]
        counts = Counter((sid, value) for sid, value, _ in candidates)
        previous = entry.get("suggested_fact_evidence")
        suggestions = [s for s in previous if isinstance(s, dict)] if isinstance(previous, list) else []
        seen = {s.get("fact_id") for s in suggestions}
        missing = []
        for sid, value, fact in candidates:
            if counts[(sid, value)] != 1:
                continue
            loc = fact["location"]
            if any(a <= loc["character_start"] and loc["character_end"] <= b for a, b in existing[sid]):
                continue
            label = f'{fact["value"]} {fact.get("unit", "")}'.strip()
            if label not in missing and len(missing) < 5:
                missing.append(label)
            if fact["fact_id"] not in seen and len(suggestions) < 5:
                seen.add(fact["fact_id"])
                suggestions.append({key: deepcopy(fact[key]) for key in
                    ("fact_id", "source_id", "value", "unit", "period", "quotation", "location") if key in fact} |
                    {"status": "requires_semantic_review"})
        if missing:
            entry["uncited_fact_values"] = missing
        if suggestions:
            entry["suggested_fact_evidence"] = suggestions
        output.append(entry)
    return output


def suggest_related_claim_quotations(claims: list[dict], sources: list[ResearchSource]) -> list[dict]:
    """Suggest other claims' uniquely located numeric excerpts, never support.

    Candidates come only from a source already cited by the target claim;
    equal numbers do not establish entity, currency, unit, period or meaning. The
    source scan is capped and is deliberately not an exhaustive search.
    """
    source_map = {s.source_id: s for s in sources if s.permissions.display and not s.is_simulated}
    output = [deepcopy(claim) for claim in claims]
    demands: dict[tuple[str, str, Decimal], dict[int, list[str]]] = defaultdict(dict)

    def located(citation: object) -> tuple[str, str, dict, ResearchSource] | None:
        if not isinstance(citation, dict) or citation.get("partial_token"):
            return None
        sid, quote, loc = citation.get("source_id"), citation.get("quotation"), citation.get("location")
        source = source_map.get(sid) if isinstance(sid, str) else None
        if (source is None or not isinstance(quote, str) or not 1 <= len(quote) <= 1000 or
                not isinstance(loc, dict) or loc.get("status") == "ambiguous"):
            return None
        a, b = loc.get("character_start"), loc.get("character_end")
        if (type(a) is not int or type(b) is not int or not 0 <= a < b <= len(source.text) or
                source.text[a:b] != quote or partial_ascii_token(source.text, a, b)):
            return None
        return sid, quote, loc, source

    def target_source(citation: object) -> str | None:
        found = located(citation)
        if found is not None:
            return found[0]
        if not isinstance(citation, dict) or citation.get("partial_token"):
            return None
        sid, quote, loc = citation.get("source_id"), citation.get("quotation"), citation.get("location")
        source = source_map.get(sid) if isinstance(sid, str) else None
        if source is None or not isinstance(quote, str) or not 1 <= len(quote) <= 1000 or not isinstance(loc, dict):
            return None
        matches = loc.get("matches")
        if loc.get("status") != "ambiguous" or not isinstance(matches, list) or not 1 <= len(matches) <= 5:
            return None
        for item in matches:
            if not isinstance(item, dict):
                continue
            a, b = item.get("character_start"), item.get("character_end")
            if (type(a) is int and type(b) is int and 0 <= a < b <= len(source.text) and
                    source.text[a:b] == quote and not partial_ascii_token(source.text, a, b)):
                return sid
        return None

    for index, claim in enumerate(output):
        claim.pop("suggested_related_quotations", None)
        citations = claim.get("citations")
        if not isinstance(citations, list):
            continue
        cited_sources = {sid for citation in citations[:10] if (sid := target_source(citation)) is not None}
        for kind, field in (("dollar", "uncited_dollar_amounts"), ("percent", "uncited_percentages")):
            missing = claim.get(field)
            if not isinstance(missing, list):
                continue
            for raw in missing[:5]:
                if not isinstance(raw, str):
                    continue
                values = [v for _, v in _dollar_amounts(raw)] if kind == "dollar" else [v for match in _PERCENT.finditer(raw) for v in _quoted_numbers(match.group(1).replace("−", "-"))]
                for value in values:
                    for sid in sorted(cited_sources):
                        demands[(sid, kind, value)].setdefault(index, []).append(raw)
    if not demands:
        return output
    checked = 0
    seen: dict[tuple[int, str, int, int], dict] = {}
    for source_index, claim in enumerate(output):
        citations = claim.get("citations")
        if not isinstance(citations, list):
            continue
        for citation in citations[:10]:
            found = located(citation)
            if found is None:
                continue
            sid, quote, loc, source = found
            if citation.get("quotation_match") in {"pdf_line_hyphen_restored", "table_markup_expanded", "split_conjunction"}:
                continue
            # A period may terminate a sentence immediately after a decimal.
            # These are candidates only: a bare number does not establish a unit.
            percent_values = _quoted_numbers(quote)
            for match in _RELATED_DECIMAL.finditer(quote):
                if len(match.group()) <= 100:
                    percent_values.add(Decimal(match.group().replace("−", "-")))
            values = {("dollar", value) for _, value in _dollar_amounts(quote)} | {("percent", value) for value in percent_values}
            matching = sorted((kind, value) for kind, value in values if (sid, kind, value) in demands)
            if not matching:
                continue
            checked += 1
            if checked > 200:
                return output
            start, end = loc["character_start"], loc["character_end"]
            if source.text.find(quote) != start or source.text.find(quote, start + 1) >= 0:
                continue
            for kind, value in matching:
                for target_index, raws in demands[(sid, kind, value)].items():
                    key = (target_index, sid, start, end)
                    if target_index == source_index:
                        continue
                    if key in seen:
                        candidate = seen[key]
                        candidate["matched_amounts"] = list(dict.fromkeys([*candidate["matched_amounts"], *raws]))[:5]
                        continue
                    target = output[target_index]
                    candidates = target.setdefault("suggested_related_quotations", [])
                    if len(candidates) >= 5:
                        continue
                    candidate = {"source_id": sid, "quotation": quote, "location": deepcopy(loc),
                                       "matched_amounts": list(dict.fromkeys(raws))[:5],
                                       "from_claim_index": source_index, "status": "requires_semantic_review"}
                    candidates.append(candidate)
                    seen[key] = candidate
    return output


def _difference_label(label: str) -> str:
    # Ignore only an explicit, trailing release qualifier; accounting basis,
    # geography and other parenthetical details remain part of the label.
    return re.sub(r"\s*[（(]\s*(?:\d{1,2}\s*月)?(?:公告|发布稿)(?:初值|修订值|修订)?\s*[）)]\s*$", "", label).strip()


def _difference_period(period: str) -> str:
    # Publication time is not the observed financial period. Do not normalize
    # free-form dates or infer the duration of fiscal or calendar periods.
    return re.sub(r"\s*[；;]\s*(?:发布于|公布于)\s*(?:19|20)\d{2}-\d{2}-\d{2}\s*$", "", period).strip()


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
        normalized_label, normalized_period = _difference_label(label), _difference_period(fact.period)
        if not normalized_label or not normalized_period:
            continue
        key = (" ".join(normalized_label.split()).casefold(), normalized_period.casefold(),
               fact.unit.casefold(), fact.currency.casefold() if fact.currency else None)
        groups[key].append((fact, item))
    result = []
    for entries in groups.values():
        if len({item["source_id"] for _, item in entries}) < 2 or len({fact.value for fact, _ in entries}) < 2:
            continue
        visible = entries[:20]
        if len(entries) > 20 and not any(
                first_item["source_id"] != second_item["source_id"] and first_fact.value != second_fact.value
                for first_fact, first_item in visible for second_fact, second_item in visible):
            # Keep an actually divergent cross-source pair visible even if
            # one source contributed the first twenty observations.
            first_fact, first_item = entries[0]
            other = next(((fact, item) for fact, item in entries
                          if item["source_id"] != first_item["source_id"] and fact.value != first_fact.value), None)
            if other is None:
                other = next((entry for entry in entries if entry[1]["source_id"] != first_item["source_id"]), None)
                first_fact, first_item = next((fact, item) for fact, item in entries if fact.value != other[0].value)
            selected_ids = {first_fact.fact_id, other[0].fact_id}
            visible = [(first_fact, first_item), other] + [entry for entry in entries if entry[0].fact_id not in selected_ids][:18]
        normalized_label = _difference_label(entries[0][1]["label"])
        normalized_period = _difference_period(entries[0][0].period)
        qualifiers_removed = any(_difference_label(item["label"]) != item["label"].strip() or
                                 _difference_period(fact.period) != fact.period.strip() for fact, item in entries)
        result.append({"label": normalized_label, "period": normalized_period,
            "unit": entries[0][0].unit, "currency": entries[0][0].currency,
            **({"matching_basis": "release_qualifiers_ignored"} if qualifiers_removed else {}),
            "status": "requires_entity_and_method_review", "truncated": len(entries) > 20,
            "facts": [{"fact_id": fact.fact_id, "source_id": item["source_id"], "value": str(fact.value),
                       "label": item["label"], "period": fact.period}
                      for fact, item in visible]})
        if len(result) == 50:
            break
    return result
