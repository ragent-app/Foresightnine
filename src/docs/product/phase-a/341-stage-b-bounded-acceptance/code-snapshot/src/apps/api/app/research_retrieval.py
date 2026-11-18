"""Bounded, domain-neutral lexical retrieval over source passages.

This is a candidate selector, not semantic matching or evidence validation.
Offsets always refer to the supplied text; no source material is rewritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import log
import re
from typing import Callable
from unicodedata import east_asian_width


_STOP = frozenset('the and for with from that this what which where when why how does did into '
                  'about using explain compare provide describe please their its are was were '
                  'have has had not but can could would should'.split())
# Unicode words let the same selector handle accented and non-Latin text.
# Wide-script runs are split into overlapping pairs without a domain lexicon.
_WORD = re.compile(r'[^\W_]+', re.UNICODE)
MAX_TERMS = 32
MAX_CHUNKS = 2500
CHUNK_SIZE = 1200


def tokenize_query(value: str) -> list[str]:
    """Same bounded lexical terms for indexing and evidence allocation."""
    groups: list[list[str]] = []
    for match in _WORD.finditer(value.lower()[:2000]):
        token = match.group()
        parts = ([token[i:i + 2] for i in range(len(token) - 1)]
                 if len(token) > 1 and east_asian_width(token[0]) in {'W', 'F'} else [token])
        group = list(dict.fromkeys(term for term in parts if len(term) >= 2 and term not in _STOP))
        if len(group) > MAX_TERMS:
            group = [group[i * (len(group) - 1) // (MAX_TERMS - 1)] for i in range(MAX_TERMS)]
        if group:
            groups.append(group)
    found: list[str] = []
    for index in range(MAX_TERMS):
        for group in groups:
            if index < len(group) and group[index] not in found:
                found.append(group[index])
                if len(found) >= MAX_TERMS:
                    return found
    return found


@dataclass(frozen=True)
class Passage:
    start: int
    end: int
    score: float
    hits: frozenset[str]
    anchor: int
    facets: frozenset[int] = frozenset()


@dataclass(frozen=True)
class PassageIndex:
    text: str
    passages: tuple[Passage, ...]
    truncated: bool
    term_weights: tuple[tuple[str, float], ...] = ()
    facet_terms: tuple[tuple[str, ...], ...] = ()

    def select(self, total_chars: int, count: int) -> list[tuple[int, str]]:
        """Return disjoint, ranked windows in source order, within the budget."""
        if total_chars <= 0 or count <= 0 or not self.passages:
            return []
        count = min(count, total_chars, len(self.passages))
        chosen: list[Passage] = []
        covered: set[str] = set()
        covered_facets: set[int] = set()
        remaining = list(self.passages)
        for _ in range(count):
            if not remaining:
                break
            # Prefer additional query coverage, while retaining lexical rank.
            best = max(remaining, key=lambda p: (p.score + 0.5 * len(p.hits - covered) +
                                                  2.0 * len(p.facets - covered_facets), -p.start))
            chosen.append(best)
            covered.update(best.hits)
            covered_facets.update(best.facets)
            remaining.remove(best)
        windows = []
        share, extra = divmod(total_chars, len(chosen))
        for i, passage in enumerate(chosen):
            length = min(passage.end - passage.start, share + (i < extra))
            if length <= 0:
                continue
            left = max(passage.start, min(passage.anchor - length // 3, passage.end - length))
            windows.append((left, self.text[left:left + length]))
        return sorted(windows, key=lambda item: item[0])


def build_passage_index(text: str, query: str, should_stop: Callable[[], bool] | None = None, *,
                        facets: list[str] | None = None) -> PassageIndex | None:
    """Index bounded chunks and rank lexical candidates without domain rules.

    The query and source may be in different languages; no match is not proof
    of irrelevance, so the caller must provide its own fallback and expose
    partial source coverage. The cancellation callback may raise.
    """
    groups = [tokenize_query(query[:2000])]
    groups.extend(tokenize_query(value[:300]) for value in (facets or [])[:12] if isinstance(value, str))
    terms: list[str] = []
    # Round-robin across question and facets: a long question must not spend
    # the entire lexical budget before any proposed subquestion is considered.
    for position in range(MAX_TERMS):
        for group in groups:
            if position < len(group) and group[position] not in terms:
                terms.append(group[position])
                if len(terms) == MAX_TERMS:
                    break
        if len(terms) == MAX_TERMS:
            break
    if not terms:
        return None
    patterns = {term: re.compile(re.escape(term), re.IGNORECASE) for term in terms}
    chunks: list[tuple[int, int, dict[str, int]]] = []
    start = 0
    while start < len(text) and len(chunks) < MAX_CHUNKS:
        if should_stop and should_stop():
            # Keep the public analysis interruption behavior in the caller.
            return None
        end = min(len(text), start + CHUNK_SIZE)
        if end < len(text):
            boundary = text.rfind('\n', start + CHUNK_SIZE // 2, end)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end]
        # Regex positions refer to the original string; Unicode casefolding
        # can change its length and would corrupt the recorded offsets.
        hits = {term: match.start() for term, pattern in patterns.items()
                if (match := pattern.search(chunk)) is not None}
        chunks.append((start, end, hits))
        start = end
    if not any(hits for _, _, hits in chunks):
        return None
    frequencies = {term: sum(term in hits for _, _, hits in chunks) for term in terms}
    weights = {term: 1.0 + log((1.0 + len(chunks)) / (1.0 + frequencies[term])) for term in terms}
    ranked = []
    for left, right, hits in chunks:
        if not hits:
            continue
        best_term = max(hits, key=lambda term: weights[term])
        facet_hits = frozenset(index for index, group in enumerate(groups[1:]) if any(term in hits for term in group))
        ranked.append(Passage(left, right, sum(weights[term] for term in hits),
                              frozenset(hits), left + hits[best_term], facet_hits))
    return PassageIndex(text, tuple(ranked), start < len(text), tuple(weights.items()),
                        tuple(tuple(term for term in group if term in weights) for group in groups[1:]))
