"""Select indivisible, traceable evidence units without asserting relevance."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .research_retrieval import tokenize_query


@dataclass(frozen=True)
class EvidenceUnit:
    kind: str
    parts: tuple[tuple[int, str], ...]


def evidence_units(text: str, check: Callable[[], None] | None = None, *, layout: bool = False) -> list[EvidenceUnit]:
    """Split prose at sentence/paragraph boundaries; preserve table headers with rows.

    Offsets always address the original text. Unrecognised pipe tables are omitted
    rather than presenting a row without its column context.
    """
    units: list[EvidenceUnit] = []
    lines = list(re.finditer(r'[^\n]*(?:\n|$)', text))
    lines = [m for m in lines if m.start() < m.end()]
    table_ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if check and i % 64 == 0:
            check()
        if lines[i].group().count('|') < 2:
            # Layout extraction may retain columns as multiple spaces.
            # Do not pass numeric multi-column rows as prose when a reliable
            # table header cannot be established.
            is_column = lambda line: bool(re.search(r'\S[ \t]{2,}\S', line))
            if layout and is_column(lines[i].group()):
                j = i
                while j < len(lines) and is_column(lines[j].group()):
                    j += 1
                group = lines[i:j]
                if len(group) >= 2 and any(len(re.findall(r'(?<!\w)\d+(?:[.,]\d+)?', m.group())) >= 2 for m in group[1:]):
                    table_ranges.append((group[0].start(), group[-1].end()))
                    # Only a contiguous column block with a period-bearing
                    # header AND explicit unit can establish a row context.
                    first_row = None
                    for k, row in enumerate(group[1:], 1):
                        preceding = ''.join(item.group() for item in group[:k])
                        period = re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)|\b(?:year|quarter|month)s?\b', preceding, re.I)
                        unit = re.search(r'%|\b(?:percent|dollars?|euros?|yen|units?|thousands?|millions?|billions?)\b', preceding, re.I)
                        numbers = re.findall(r'(?<![\w.])[-+]?\d[\d,.]*', row.group())
                        if period and unit and len(numbers) >= 2 and re.search(r'[A-Za-z\u3400-\u9fff]', row.group()):
                            first_row = k
                            break
                    if first_row is not None:
                        header = text[group[0].start():group[first_row - 1].end()].rstrip('\n')
                        for row in group[first_row:]:
                            units.append(EvidenceUnit('table', ((group[0].start(), header),
                                                                (row.start(), row.group().rstrip('\n')))))
                    i = j
                    continue
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].group().count('|') >= 2:
            j += 1
        group = lines[i:j]
        table_ranges.append((group[0].start(), group[-1].end()))
        # A Markdown separator or an explicit period header establishes
        # where column context ends. Multiple header lines stay together.
        separator = next((k for k, line in enumerate(group)
                          if k > 0 and re.fullmatch(r'[\s|:\-]+', line.group())), None)
        first_row = separator + 1 if separator is not None else None
        if first_row is None:
            for k, line in enumerate(group):
                cells = [cell.strip() for cell in line.group().strip().strip('|').split('|')]
                numbers = [cell for cell in cells if re.fullmatch(r'[($€£¥+\-\s]*\d[\d,.]*[)%\s]*', cell)]
                # A year-only row is column context, not an observation.
                if k and numbers and any(not re.fullmatch(r'(?:19|20)\d{2}', cell) for cell in numbers):
                    head = ''.join(item.group() for item in group[:k])
                    if re.search(r'(?<!\d)(?:19|20)\d{2}(?!\d)', head):
                        first_row = k
                    break
        if first_row is not None and first_row < len(group):
            header_start = group[0].start()
            header_end = group[first_row - 1].end()
            header_text = text[header_start:header_end].rstrip('\n')
            for row in group[first_row:]:
                parts = ((header_start, header_text),
                         (row.start(), text[row.start():row.end()].rstrip('\n')))
                units.append(EvidenceUnit('table', parts))
        i = j
    # Treat table ranges as hard breaks, never joining prose across them.
    boundaries = [(0, 0), *table_ranges, (len(text), len(text))]
    for (_, left), (right, _) in zip(boundaries, boundaries[1:]):
        block = text[left:right]
        for match in re.finditer(r'[^\n]+(?:\n(?!\s*\n)[^\n]+)*', block):
            if check:
                check()
            start, end = left + match.start(), left + match.end()
            paragraph = text[start:end]
            if len(re.findall(r'[。！？]|[.!?](?=\s|$)', paragraph)) > 1:
                units.append(EvidenceUnit('paragraph', ((start, paragraph),)))
            pos = 0
            for boundary in re.finditer(r'[。！？]|[.!?](?=\s|$)', paragraph):
                finish = boundary.end()
                prefix = paragraph[pos:finish]
                if re.search(r'\b(?:[A-Za-z]\.){2,}$', prefix) or re.search(
                        r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc|e\.g|i\.e)\.$', prefix, re.I):
                    continue
                piece = paragraph[pos:finish]
                leading = len(piece) - len(piece.lstrip())
                if piece.strip():
                    units.append(EvidenceUnit('sentence', ((start + pos + leading, piece.strip()),)))
                pos = finish
            tail = paragraph[pos:]
            if tail.strip():
                leading = len(tail) - len(tail.lstrip())
                units.append(EvidenceUnit('paragraph', ((start + pos + leading, tail.strip()),)))
    return sorted(units, key=lambda unit: unit.parts[-1][0])


def query_terms(value: str) -> list[str]:
    return tokenize_query(value)


def lexical_signature(unit: EvidenceUnit, query: str,
                      term_weights: dict[str, float] | None = None) -> frozenset[str]:
    """The index's exact terms matched in the actual unit, not semantic proof."""
    body = ' '.join(text for _, text in unit.parts).lower()
    return frozenset(term for term in (term_weights if term_weights is not None else tokenize_query(query))
                     if term in body)

def rank_units(units: list[EvidenceUnit], windows: list[tuple[int, str]],
               query: str = '', facets: list[str] | None = None, check: Callable[[], None] | None = None,
               covered_text: str = '', term_weights: dict[str, float] | None = None,
               indexed_facets: tuple[tuple[str, ...], ...] | None = None) -> list[EvidenceUnit]:
    """Legacy diagnostic helper; production allocation uses one global objective."""
    weights = term_weights if term_weights is not None else {term: 1.0 for term in tokenize_query(query)}
    facet_terms = indexed_facets if indexed_facets is not None else tuple(tuple(tokenize_query(facet)) for facet in (facets or [])[:12])
    covered = covered_text.lower()
    def priority(unit: EvidenceUnit) -> tuple[int, float, int, int, int]:
        if check:
            check()
        body = ' '.join(part for _, part in unit.parts).lower()
        # A ledger of newly covered lexical terms per source. These are
        # retrieval hints only, not proof that a requirement was met.
        hits = sum(weight for term, weight in weights.items() if term in body and term not in covered)
        facet_hits = len({tuple(sorted(group)) for group in facet_terms
                          if group and any(term in body and term not in covered for term in group)})
        index = next((index for index, (start, passage) in enumerate(windows)
                      if any(part_start < start + len(passage) and start < part_start + len(part)
                             for part_start, part in unit.parts)), len(windows))
        # Approximate serialization overhead determines ordering only; the
        # caller still accepts/rejects with exact serialized UTF-8 bytes.
        cost = sum(len(part.encode('utf-8')) + 64 for _, part in unit.parts)
        gain_per_byte = hits * hits / max(1, cost)
        return -facet_hits, -hits, -gain_per_byte, index, unit.parts[-1][0]
    return sorted(units, key=priority)


def allocation_score(signatures: dict[str, set[str]], source_weights: dict[str, dict[str, float]],
                     facets: list[set[str]], eligible_source_count: int) -> float:
    """One global lexical objective; shared facet terms contribute only once per source.

    Scores express retrieval signal only. They do not establish task completion,
    semantic support, source independence, or facts absent from the source.
    """
    frequencies = {term: sum(term in facet for facet in facets) for facet in facets for term in facet}
    value = 0.0
    for sid, matched in signatures.items():
        weights = source_weights.get(sid, {})
        for facet in facets:
            raw = sum(max(0.0, weights.get(term, 0.0)) / frequencies[term] for term in matched.intersection(facet))
            value += raw / (1.0 + raw)
    return value / max(1, eligible_source_count)
