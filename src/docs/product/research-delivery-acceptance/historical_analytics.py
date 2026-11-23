#!/usr/bin/env python3
"""Recheck a read-only historical research snapshot without providers or writes.

Run: src/apps/api/.venv/bin/python src/docs/product/research-delivery-acceptance/historical_analytics.py /tmp/fn-delivery-XqMfDSDb
The snapshot is historical evidence only; it is not a live market-data assertion.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'apps' / 'api'))
from app.research_charts import CreateChartRequest, build_research_chart
from app.research_sources import ResearchSource
from app.research_run_insights import evaluate_method_and_reconciliation
from app.research_return_context import price_return_context


def pct(end: str, start: str) -> str:
    return str(((Decimal(end) / Decimal(start) - 1) * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('snapshot', type=Path)
    args = parser.parse_args()
    histories = [json.loads(line) for line in (args.snapshot / 'history.jsonl').read_text().splitlines() if line.strip()]
    sources = {row['source_id']: ResearchSource.model_validate(row) for row in
               (json.loads(line) for line in (args.snapshot / 'sources.jsonl').read_text().splitlines() if line.strip())}
    versions = [v for history in histories for v in history.get('versions', [])]
    candidate = next(v for v in versions if v.get('content', {}).get('charts') and v['content'].get('facts'))
    content = candidate['content']
    facts = content['facts']
    def fact(label: str):
        return next(f for f in facts if f.get('label') == label)
    start, opening, close, next_close = (fact(x) for x in ('IPO 发行价', 'ANT 上市首日开盘价', 'ANT 上市首日收盘价', 'ANT 第二日收盘价'))
    returns = {'issue_to_close': pct(close['value'], start['value']), 'open_to_close': pct(close['value'], opening['value']), 'close_to_next_close': pct(next_close['value'], close['value'])}
    assert returns == {'issue_to_close': '17.07', 'open_to_close': '-12.85', 'close_to_next_close': '-8.50'}
    fact_map = {f['fact_id']: f for f in facts}
    def context(first, second):
        return price_return_context({'formula_id': 'growth_percent', 'inputs': [first, second]}, fact_map, sources)
    contexts = {'issue_to_close': context(close, start), 'open_to_close': context(close, opening), 'close_to_next_close': context(next_close, close)}
    assert contexts['issue_to_close']['start_date'] == '2026-11-18'
    assert contexts['open_to_close']['start_price_type'] == 'open' and contexts['open_to_close']['end_price_type'] == 'close'
    assert contexts['close_to_next_close']['start_price_type'] == 'close'

    old = next(c for c in content['charts'] if len(c.get('series', [])) >= 2)
    old_starts = {s['name']: s['points'][0]['x'] for s in old['series']}
    assert len(set(old_starts.values())) > 1
    bars = [s for s in sources.values() if s.kind == 'market_data' and s.title.startswith(('ANT@NASDAQ 1day bars', 'QQQ@NASDAQ 1day bars'))]
    by_symbol = {s.title.split('@')[0]: s for s in bars}
    request = CreateChartRequest.model_validate({'chart_type': 'price_series', 'title': 'ANT vs QQQ', 'y_axes': [{'id': 'left', 'label': 'Index'}], 'series': [{'name': symbol, 'mark': 'line', 'binding': {'kind': 'historical_prices', 'source_id': by_symbol[symbol].source_id, 'normalize_base_100': True}} for symbol in ('ANT', 'QQQ')], 'rationale': 'Compare common closes'})
    chart = build_research_chart(request, facts=[], calculations=[], sources={s.source_id: s for s in by_symbol.values()})
    assert chart.normalization_base == '2026-11-19'
    assert all(s.points[0].x == chart.normalization_base and Decimal(s.points[0].y) == 100 for s in chart.series)
    turn = next(t for t in content['turns'] if sum(e.get('name') == 'search_source' for e in t.get('events', [])) >= 6)
    counts = Counter(e.get('name') for e in turn['events'] if e.get('kind') == 'tool')
    assert (counts['news_search'], counts['search_source']) == (2, 6)
    assessment = evaluate_method_and_reconciliation({'events': turn['events']}, sources)['counter_evidence']
    assert assessment['completed_actions'] == 0  # legacy events lack typed counter_search intent
    print(json.dumps({'returns_percent': returns, 'return_context': contexts, 'old_chart_start_dates': old_starts,
                      'corrected_common_base': chart.normalization_base,
                      'corrected_series': {s.name: [{'date': p.x, 'value': p.y} for p in s.points] for s in chart.series},
                      'historical_search_events': {'external': counts['news_search'], 'document': counts['search_source']},
                      'explicit_counter_searches': assessment['completed_actions'],
                      'counter_search_note': 'Historic events lack typed intent; total searches do not establish counter-evidence attempts.',
                      'live_calls': 0}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
