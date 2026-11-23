"""Offline scripted runtime, real turn persistence merge, and delivery replay."""
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from app.research_runtime import run_research_agent
from app.research_turn_persister import ResearchTurnPersister
from app.research_topics import ResearchContent, ResearchVersion
from tests.test_research_runtime_attention import _source, _ctx, _tool_response, NativeTurnRecorder

ROOT = Path(__file__).resolve().parent
fixture = json.loads((ROOT / 'frozen-cases.json').read_text())
now = datetime(2026, 4, 1, tzinfo=timezone.utc)
results = {}
for case, note_key, mode in [('F1','increment_note','correct'),('F2','current_note','incorrect'),('F3',None,'omit')]:
    statement = fixture['financial']['statement']
    texts = [statement] + ([fixture['financial'][note_key]] if note_key else [])
    sources = [_source(s['id'], s['text']) for s in texts]
    for src in sources:
        src.obtained_at = now
        src.published_at = datetime(2026,2,1,tzinfo=timezone.utc)
        src.content_sha256 = sha256(src.text.encode()).hexdigest()
    question = fixture['financial']['question']
    saved = []
    rows = [{'label':'Northport revenue','value':v,'unit':'USD millions','currency':'USD','period':p,'source_id':statement['id'],'quotation':statement['text']} for p,v in [('FY2025',120),('FY2024',100)]]
    def calc(_turn,_index):
        facts=saved[-1]['facts']
        return _tool_response([('calculate_financials',{'calculations':[{'formula':'growth_percent','first_fact_id':facts[0]['fact_id'],'second_fact_id':facts[1]['fact_id']}],'rationale':'recompute growth'},'calc')])
    read = _tool_response([('read_sources',{'reads':[{'source_id':s['id']} for s in texts],'rationale':'read authorized materials'},'read')])
    factcall = _tool_response([('record_facts',{'facts':rows,'rationale':'record both annual figures'},'facts')])
    claims = [{'text':'Northport revenue rose from 100 to 120 million USD.','source_id':statement['id'],'quotation':statement['text'],'role':'support'}]
    if note_key:
        claims.append({'text':'The acquired business contributed USD 18 million of the increase in consolidated revenue.','source_id':texts[1]['id'],'quotation':texts[1]['text'],'role':'support'})
    submit = {'report':'Northport revenue rose 20%.' + (' The acquisition contributed 90% of the revenue increase.' if note_key else ''),'claims':claims,'limitations':['Sustainability is unknown.'],'stopping_reason':'answered'}
    recorder = NativeTurnRecorder([read, factcall, calc, *[_tool_response([('submit_research',submit,f'submit-{i}')]) for i in range(4)]])
    ctx = _ctx(max_calls=7)
    try:
        out = run_research_agent(question=question,sources=sources,model=recorder,context=ctx,as_of=now,checkpoint=lambda state:saved.append(json.loads(json.dumps(state))))
        state=saved[-1]
        meta={'turn_id':'turn-'+case,'index':1,'question':question,'question_origin':'user','operation_ids':[],'parent_version_id':'parent','started_at':now}
        parent=ResearchVersion(version_id='parent',created_at=now,content=ResearchContent(),review='draft')
        persister=ResearchTurnPersister(topic_id='topic',parent_version_id='parent',as_of=now,sources=sources,version_chain=SimpleNamespace(source_ids=[]),turn_meta=meta)
        finalized=persister.finalize_turn(operation_id='op-'+case,parent_version=parent,outcome=out,runtime_state=state,context=ctx,finished_at=now,default_search_status='not_requested',official_fetch_status='not_requested')
        results[case]={'status':out.status,'read_ranges':state.get('read_ranges'), 'events':out.events, 'runtime_facts':out.facts,'runtime_calculations':out.calculations,'runtime_claims':out.claims,'runtime_parts':out.question_parts,'persisted_content':finalized.content.model_dump(mode='json')}
    except Exception as exc:
        results[case]={'error':repr(exc),'checkpoint':saved[-1] if saved else None}

# Render the frozen observations as the existing market adapter's persisted bar format.
from tests.test_research_market_analytics import _market_source
for variant in fixture['market']['variants']:
    case=variant['id']
    grouped={}
    for symbol, day, price in variant['observations']:
        grouped.setdefault(symbol, []).append(f"{symbol}@X | {day} | 1d | O {price} H {price} L {price} C {price} V 100 USD ADJF 1")
    sources=[_market_source(symbol.lower(), '\n'.join(lines)) for symbol,lines in grouped.items()]
    for src in sources:
        src.obtained_at=now
        src.published_at=datetime(2026,2,5,tzinfo=timezone.utc)
        src.content_sha256=sha256(src.text.encode()).hexdigest()
    question=fixture['market']['question']
    state_history=[]
    req={'source_id':'gamma','benchmark_source_id':'benchmark','price_basis':'adjusted','benchmark_selection_reason':'Frozen comparison benchmark.'}
    submit={'report':'Gamma adjusted closing price rose.','claims':[{'text':'Gamma adjusted closing price was 12 USD.','source_id':'gamma','quotation':sources[0].text.splitlines()[-1],'role':'support'}], 'limitations':['Benchmark may be incomplete.'],'stopping_reason':'answered'}
    script=[_tool_response([('read_sources',{'reads':[{'source_id':s.source_id} for s in sources],'rationale':'read'},'read')]), _tool_response([('analyze_market_series',req,'market')])] + [_tool_response([('submit_research',submit,f'submit-{i}')]) for i in range(4)]
    recorder=NativeTurnRecorder(script)
    ctx=_ctx(max_calls=6)
    try:
        out=run_research_agent(question=question,sources=sources,model=recorder,context=ctx,as_of=now,checkpoint=lambda state:state_history.append(json.loads(json.dumps(state))))
        state=state_history[-1]
        parent=ResearchVersion(version_id='parent',created_at=now,content=ResearchContent(),review='draft')
        meta={'turn_id':'turn-'+case,'index':1,'question':question,'question_origin':'user','operation_ids':[],'parent_version_id':'parent','started_at':now}
        persister=ResearchTurnPersister(topic_id='topic',parent_version_id='parent',as_of=now,sources=sources,version_chain=SimpleNamespace(source_ids=[]),turn_meta=meta)
        finalized=persister.finalize_turn(operation_id='op-'+case,parent_version=parent,outcome=out,runtime_state=state,context=ctx,finished_at=now,default_search_status='not_requested',official_fetch_status='not_requested')
        results[case]={'status':out.status,'read_ranges':state.get('read_ranges'),'events':out.events,'market_analyses':state.get('market_analyses'),'runtime_claims':out.claims,'runtime_parts':out.question_parts,'persisted_content':finalized.content.model_dump(mode='json')}
    except Exception as exc:
        results[case]={'error':repr(exc),'checkpoint':state_history[-1] if state_history else None}
(ROOT/'replay-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str)+'\n')
print(json.dumps({k:{'error':v.get('error'),'status':v.get('status'),'facts':len(v.get('runtime_facts',[])), 'claims':len(v.get('runtime_claims',[])), 'parts':v.get('runtime_parts')} for k,v in results.items()},ensure_ascii=False,indent=2))
