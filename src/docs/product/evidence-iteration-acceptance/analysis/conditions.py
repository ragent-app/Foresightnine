"""Run four submissions against the same contract and recompute a shortened benchmark window."""
from datetime import datetime, timezone
from hashlib import sha256
import json

from app.research_delivery import assess, assemble_parts
from app.research_market_analytics import AnalyzeMarketSeriesRequest, analyze_market_series
from app.research_runtime import run_research_agent
from tests.test_research_market_analytics import _market_source
from tests.test_research_runtime_attention import _ctx, _source, _tool_response, NativeTurnRecorder

now = datetime(2026, 4, 1, tzinfo=timezone.utc)
clause = '买方在供应商实质违约且书面通知后三十日仍未补救时方可提前撤销订单。'
exception = '但已验收的批次除外。'
full = clause + exception
cases = [
    ('qualified', full, full, True),
    ('party_swapped', full.replace('买方', '供应商', 1), full, False),
    ('exception_dropped', clause, full, False),
    ('truncated_citation', clause, clause, False),
]
rows = []
for label, claim, quotation, expected in cases:
    source = _source('clause', full)
    source.content_sha256 = sha256(source.text.encode()).hexdigest()
    source.obtained_at = source.published_at = now
    submission = {'report': claim, 'claims': [{'text': claim, 'source_id': 'clause', 'quotation': quotation, 'role': 'support'}],
                  'status': 'partial', 'limitations': ['交货状态另待核实。'], 'stopping_reason': 'answered'}
    recorder = NativeTurnRecorder([
        _tool_response([('read_sources', {'reads': [{'source_id': 'clause'}], 'rationale': '阅读全文'}, 'read')]),
        _tool_response([('submit_research', submission, 'submit')])])
    result = run_research_agent(question='谁可以撤销订单，有哪些前提？', sources=[source], model=recorder, context=_ctx(max_calls=2), as_of=now)
    accepted = any(c.get('text') == claim for c in result.claims)
    assert accepted == expected, (label, result.claims)
    assert (claim in (result.partial_notes or '')) == expected, label
    rows.append({'case': label, 'claim_retained': accepted, 'status': result.status})

sources = {}
for symbol, observations in [('GAM', [(2, 10), (3, 11), (4, 12)]), ('IDX', [(3, 100), (4, 110)])]:
    text = '\n'.join(f'{symbol}@X | 2026-02-{d:02} | 1d | O {n} H {n} L {n} C {n} V 10 USD ADJF 1' for d, n in observations)
    src = _market_source(symbol, text)
    src.obtained_at = src.published_at = now
    src.content_sha256 = sha256(text.encode()).hexdigest()
    sources[symbol] = src
request = AnalyzeMarketSeriesRequest(source_id='GAM', benchmark_source_id='IDX', start_date='2026-02-02', end_date='2026-02-04')
data = analyze_market_series(request, sources=sources, as_of=now)
state = {'market_analyses': [{'request': request.model_dump(), 'analysis_id': 'a1', 'result': data}]}
q = 'Compare GAM with benchmark IDX from 2026-02-02 to 2026-02-04.'
parts = assemble_parts(q, state, sources, set(sources), assess(q, state, sources, as_of=now), [])
assert data['benchmark']['missing_requested_endpoints'] == ['2026-02-02']
assert any(p['question'] == q and p['status'] == 'unresolved' for p in parts)
assert any(p['status'] == 'answered' and '2026-02-03' in p['answer'] and '-0.91' in p['answer'] for p in parts), parts
print(json.dumps({'contract': rows, 'market': parts}, ensure_ascii=False, indent=2))
