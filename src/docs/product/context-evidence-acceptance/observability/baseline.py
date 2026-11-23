"""Offline native-wire replay of F1/M1. No network or quality inference."""
import json
import os
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from app.anthropic_messages import AnthropicMessagesProvider
from app.openai_compatible import HttpResponse
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions

for key in ('FORESIGHTNINE_EMBEDDING_BASE_URL', 'FORESIGHTNINE_EMBEDDING_MODEL', 'FORESIGHTNINE_EMBEDDING_API_KEY'):
    os.environ.pop(key, None)

HERE = Path(__file__).resolve().parent
fixture_bytes = (HERE.parent / 'evidence/frozen-cases.json').read_bytes()
fixture = json.loads(fixture_bytes)
result = {'fixture_sha256': sha256(fixture_bytes).hexdigest(), 'baseline_head': '3e1ca4d458d9a6ccc479c2d7123d5a81ba998d70', 'cases': {}}
for case, rows, arm in [(case, rows, arm) for case, rows in [('F1', [fixture['financial']['statement'], fixture['financial']['increment_note']]), ('M1', [fixture['market']['source']])] for arm in ('submitted', 'truncated')]:
    question = fixture['financial' if case == 'F1' else 'market']['question']
    sources = [ResearchSource(source_id=r['id'], owner_id='fixture', title=r['id'], text=r['text'], kind='user_supplied', published_at=datetime.fromisoformat(r['published']).replace(tzinfo=timezone.utc), obtained_at=datetime(2026, 3, 1, tzinfo=timezone.utc), content_sha256=sha256(r['text'].encode()).hexdigest(), permissions=SourcePermissions(store=True, model_input=True, display=True, export=True)) for r in rows]
    requests = []
    def transport(method, url, headers, body, timeout):
        requests.append(body)
        if len(requests) == 1:
            blocks = [{'type':'tool_use', 'id':'search-1', 'name':'search_library', 'input':{'query': 'revenue FY2025 FY2024' if case == 'F1' else 'Gamma Benchmark adjusted close', 'max_hits': 8}}]
            reason = 'tool_use'
        elif len(requests) == 2:
            blocks = [{'type':'tool_use', 'id':'read-1', 'name':'read_sources', 'input':{'reads':[{'source_id':s.source_id} for s in sources], 'rationale':'Read frozen exhibit'}}]
            reason = 'tool_use'
        elif arm == 'submitted':
            row = rows[0]
            blocks = [{'type':'tool_use', 'id':'submit-1', 'name':'submit_research', 'input':{'status':'partial', 'report':'Located exhibit; further analysis is needed.', 'claims':[{'text':row['text'], 'source_id':row['id'], 'quotation':row['text'], 'role':'support'}], 'limitations':['Further analysis is needed.'], 'stopping_reason':'partial'}}]
            reason = 'tool_use'
        else:
            blocks = [{'type':'text', 'text':'fixture interruption'}]
            reason = 'max_tokens'
        return HttpResponse(200, json.dumps({'role':'assistant','content':blocks,'stop_reason':reason,'usage':{'input_tokens':0,'output_tokens':0}}))
    settings = SimpleNamespace(base_url='https://example.com', model='fixture-model', api_key='unused', allow_private_network=False)
    policy = SimpleNamespace(timeout_seconds=10, max_input_tokens=None, max_output_tokens=256)
    provider = AnthropicMessagesProvider(settings, policy, anthropic_version='2023-06-01', auth_scheme='bearer', max_tokens=256, transport=transport)
    context = ResearchExecutionContext(ResearchExecutionBudget(3, 0, None, time.monotonic()+120), False, lambda:None, lambda:None, lambda *_:None, lambda *_:None, lambda:None, diagnostic_context={'operation_id':'fixture-'+case})
    outcome = run_research_agent(question=question, sources=sources, model=provider, context=context, as_of=datetime(2026,4,1,tzinfo=timezone.utc))
    diagnostics = list(context.private_diagnostics.values())
    search = next(r for d in diagnostics for r in d.get('tool_evidence_receipts', []) if r['tool'] == 'search_library')
    assert 'selection_trace' in search and 'requirement_queries' in search
    assert b'selection_trace' not in requests[1] and b'requirement_queries' not in requests[1]
    read = next(r for d in diagnostics for r in d.get('tool_evidence_receipts', []) if r['tool'] == 'read_sources')
    expected_ids = {s.source_id for s in sources}
    assert {c['source_id'] for c in search['candidates']} == expected_ids
    assert {r['source_id'] for r in read['read_spans']} == expected_ids
    assert any({r['source_id'] for r in entry['read_spans']} == expected_ids
               for d in diagnostics for entry in d.get('input_tool_references', []))
    deliveries = [d['delivery'] for d in diagnostics if 'delivery' in d]
    assert len(deliveries) == 1
    assert deliveries[0]['association'] == ('submit_tool' if arm == 'submitted' else 'last_observed_call')
    result['cases'][case + '-' + arm] = {'outcome':outcome.status, 'reason':outcome.reason, 'transport_calls':len(requests), 'diagnostics':context.private_diagnostics}
print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
