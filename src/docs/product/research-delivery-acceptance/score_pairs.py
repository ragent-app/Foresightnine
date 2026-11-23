"""Summarize recorded runs against the two fixed exhibits, without model or search calls."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path

from paired import CASES


def located(row, source):
    bounds = row.get('location') or row
    start, end = bounds.get('character_start'), bounds.get('character_end')
    return (row.get('source_id') == 'exhibit' and isinstance(start, int) and isinstance(end, int)
            and 0 <= start < end <= len(source) and source[start:end] == row.get('quotation'))


def score(path):
    row = json.loads(path.read_text())
    source = CASES[row['case']][0]
    facts = [f for f in row['facts'] if located(f, source)]
    citations = [c for claim in row['claims'] for c in claim['citations']]
    result = {'run':path.stem, 'status':row['status'], 'reason':row['reason'], 'model_calls':row['actual_calls'],
              'search_calls':row['search_calls'], 'reported_tokens':row['usage']['reported_tokens'],
              'unknown_calls':row['usage']['unknown_usage_calls'], 'first_request_bytes':row['diagnostics'][0]['wire_bytes'],
              'elapsed_seconds':row['elapsed_seconds'], 'seed_facts':row['seed_facts'], 'located_facts':len(facts),
              'located_claim_citations':sum(located(c, source) for c in citations), 'claims':len(row['claims']),
              'answered_parts':sum(p['status']=='answered' for p in row['question_parts'])}
    if row['case']=='recovery':
        result['employee_days_recorded'] = any(Decimal(f['value']) == 90 and 'employee shares are locked for 90 days' in f['quotation'].lower() for f in facts)
        result['public_holding_disclosure_cited'] = any(located(c, source) and 'No holding period is disclosed for public investors.' in c['quotation'] for c in citations)
    else:
        correct = {}
        for f in facts:
            q = f['quotation']
            for year, millions in (('FY2025', 120), ('FY2024', 100)):
                if (f'{year} revenue was USD {millions} million.' in q and 'revenue' in f['label'].lower()
                        and Decimal(f['value']) == millions and 'million' in f['unit'].lower() and f.get('currency') == 'USD'):
                    correct[f['fact_id']] = year
        result['correct_revenue_periods'] = len(set(correct.values()))
        result['correct_growth_calculation'] = any(
            c['formula_id'] == 'growth_percent' and Decimal(c['value']) == 20 and c['unit'] == 'percent'
            and [correct.get(i['fact_id']) for i in c['inputs']] == ['FY2025', 'FY2024'] for c in row['calculations'])
    return result


if __name__ == '__main__':
    dest = Path(__file__).resolve().parent / 'paired-results'
    index = json.loads((dest/'index.json').read_text())
    rows = [score(dest/i['run']) for i in index['runs']]
    print(json.dumps({'runs':rows, 'total_calls':sum(r['model_calls'] for r in rows),
                      'total_reported_tokens':sum(r['reported_tokens'] for r in rows),
                      'unknown_usage_calls':sum(r['unknown_calls'] for r in rows),
                      'search_calls':sum(r['search_calls'] for r in rows),
                      'status_counts':dict(Counter(r['status'] for r in rows)), 'actual_cost':None}, indent=2))
