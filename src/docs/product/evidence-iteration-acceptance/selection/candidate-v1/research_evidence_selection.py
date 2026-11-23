"""Bounded, unverified retrieval-lead combination selection.

This module does not read evaluation labels or establish evidentiary support.
"""
from __future__ import annotations

import itertools
import re
from typing import Any, Mapping


def evidence_uses(requirement: Mapping[str, Any]) -> list[str]:
    kind = str(requirement.get('kind') or '').lower()
    q = str(requirement.get('question') or '').lower()
    if kind.startswith('counterevidence'):
        return ['counter']
    if kind.startswith('market:') or re.search(r'benchmark|index|relative return|指数|同期表现', q):
        return ['market_first', 'market_second']
    if re.search(r'contract|cancel|termination|delivery|合同|撤单|交货', q):
        return ['condition', 'performance']
    if kind.startswith('contribution:') or re.search(r'drivers?|contribution|attribution|原因|归因|贡献', q):
        return ['periods', 'attribution']
    if kind.startswith('financial:') or re.search(r'growth|change|revenue|增长|变化|收入|营收', q):
        return ['periods']
    return ['fact']


def lead_uses(text: str) -> set[str]:
    t = text.lower()
    uses = {'fact'}
    if len(set(re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)', t))) >= 2:
        uses.add('periods')
    if re.search(r'price|pricing|volume|contribut|driven|due to|attribut|价格|销量|贡献|驱动|由于', t):
        uses.add('attribution')
    if re.search(r'cancel|terminat|notice|unless|remed|合同|撤销|撤单|通知|补救|除非', t):
        uses.add('condition')
    if re.search(r'deliver|shipment|backlog|fulfil|履约|交付|交货|订单', t):
        uses.add('performance')
    if re.search(r'index|benchmark|指数|基准', t) and re.search(r'\d', t):
        uses.add('market_second')
    if re.search(r'share|stock|equity|股票|股价|收盘价', t) and re.search(r'\d', t):
        uses.add('market_first')
    if re.search(r'risk|declin|uncertain|however|although|风险|下滑|但是|然而|不确定', t):
        uses.add('counter')
    return uses


def select_evidence_candidates(candidates: list[dict[str, Any]], requirements: list[Mapping[str, Any]], *,
                               max_hits: int, max_bytes: int | None = None, beam_width: int = 32,
                               per_use: int = 12) -> tuple[list[int], dict[str, Any]]:
    """Select indices into an already authorized pool. Metadata matches remain unverified.

    Each candidate supplies text, source_id, start, end, rank and optionally explicit
    entity/period/currency metadata. Unknown compatibility is never inferred as proof.
    """
    needed = [set(evidence_uses(r)) for r in requirements]
    roles = [lead_uses(str(c.get('text') or '')) for c in candidates]
    shortlist: set[int] = set(range(min(per_use, len(candidates))))
    for role in set().union(*needed, {'counter'}):
        shortlist.update([i for i, uses in enumerate(roles) if role in uses][:per_use])
    pool = sorted(shortlist)
    def valid(group: tuple[int, ...]) -> bool:
        if len({str(candidates[i].get('text') or '') for i in group}) < len(group):
            return False
        for field in ('entity', 'currency', 'period_scope'):
            values = {str(candidates[i][field]) for i in group if candidates[i].get(field)}
            if len(values) > 1 and not (field == 'entity' and any('market_second' in n for n in needed)):
                return False
        return max_bytes is None or byte_cost(group) <= max_bytes
    def byte_cost(group: tuple[int, ...]) -> int:
        # Count overlapping intervals once, against actual source text slices.
        total = 0
        intervals: dict[str, list[tuple[int, int, str]]] = {}
        for i in group:
            c = candidates[i]
            intervals.setdefault(str(c.get('source_id')), []).append((int(c.get('start', 0)), int(c.get('end', 0)), str(c.get('text') or '')))
        for rows in intervals.values():
            end = -1
            for start, stop, text in sorted(rows):
                total += len(text[max(0, end-start):].encode('utf-8'))
                end = max(end, stop)
        return total
    def score(group: tuple[int, ...]) -> tuple:
        found = set().union(*(roles[i] for i in group)) if group else set()
        complete = sum(n <= found for n in needed)
        covered = sum(len(n & found) for n in needed)
        counter = int('counter' in found and any('counter' in n for n in needed))
        return (complete, covered, counter, len(group), -sum(int(candidates[i].get('rank', i)) for i in group), -byte_cost(group))
    best: tuple[int, ...] = ()
    if max_hits <= 2:
        groups = itertools.chain.from_iterable(itertools.combinations(pool, size) for size in range(1, max_hits + 1))
        best = max((g for g in groups if valid(g)), key=score, default=())
    else:
        beam = [()]
        for _ in range(max_hits):
            extensions = {g + (i,) for g in beam for i in pool if (not g or i > g[-1]) and valid(g + (i,))}
            if not extensions:
                break
            beam = sorted(extensions, key=score, reverse=True)[:beam_width]
            best = max([best, *beam], key=score)
    return list(best), {'pool_size': len(candidates), 'shortlist_size': len(pool),
                        'pruned_count': len(candidates)-len(pool), 'uses': [sorted(r) for r in roles],
                        'needed_uses': [sorted(n) for n in needed], 'bytes': byte_cost(best),
                        'match_status': 'retrieval_only_unverified'}
