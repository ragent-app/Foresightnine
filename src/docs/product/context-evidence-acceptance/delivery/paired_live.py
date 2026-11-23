"""One isolated native run. Inputs come from the authored replication suite."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from app.anthropic_messages import AnthropicMessagesProvider
from app.openai_compatible import OpenAICompatibleSettings
from app.phase3 import ProviderPolicy
from app.research_budget import ResearchExecutionBudget
from app.research_coordinator import ResearchExecutionContext
from app.research_runtime import run_research_agent
from app.research_sources import ResearchSource, SourcePermissions
from app.research_topics import ResearchContent, ResearchVersion
from app.research_turn_persister import ResearchTurnPersister
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent / 'independent/replication'
CLOCK = datetime(2026, 11, 22, tzinfo=timezone.utc)


def run(case, arm):
    prefix = 'FORESIGHTNINE_RESEARCH_LLM_'
    settings = OpenAICompatibleSettings.from_values(model=os.environ[prefix+'MODEL'], base_url=os.environ[prefix+'BASE_URL'],
        allowlist=os.environ[prefix+'ALLOWED_HOSTS'], api_key=os.environ[prefix+'API_KEY'])
    assert os.environ['FORESIGHTNINE_RESEARCH_MODEL_PROTOCOL'] == 'anthropic_messages' and settings.api_key
    assert os.environ['FORESIGHTNINE_MESSAGES_CAPABILITY_BASE_URL'].rstrip('/') == settings.base_url
    assert os.environ['FORESIGHTNINE_MESSAGES_CAPABILITY_MODEL'] == settings.model
    assert all(os.environ['FORESIGHTNINE_MESSAGES_'+key] == 'true' for key in ('NATIVE_TOOLS', 'CONTINUATION'))
    model = AnthropicMessagesProvider(settings, ProviderPolicy(max_output_tokens=4096, timeout_seconds=80),
        anthropic_version=os.environ['FORESIGHTNINE_MESSAGES_VERSION'], auth_scheme=os.environ['FORESIGHTNINE_MESSAGES_AUTH_SCHEME'],
        max_tokens=4096, stream_enabled=True)
    raw = (SUITE / 'materials.json').read_bytes()
    assert sha256(raw).hexdigest() == json.loads((SUITE / 'freeze.json').read_text())['materials.json']
    sample = next(r for r in json.loads(raw) if r['id'] == case)
    sources = [ResearchSource(source_id=sid, owner_id='offline-exercise', title=title, text=text, kind='user_supplied',
                 obtained_at=CLOCK,published_at=CLOCK, content_sha256=sha256(text.encode()).hexdigest(),
                 permissions=SourcePermissions(store=True, model_input=True, display=True, export=True))
               for sid,title,text in sample['sources']]
    start=time.monotonic()
    def disabled():
        raise AssertionError('External search disabled')
    ctx=ResearchExecutionContext(ResearchExecutionBudget(6,0,180000,start+160),False,
        lambda:None,lambda:None,lambda *_:None,lambda *_:None,disabled)
    saved={}
    def checkpoint(state):
        saved.update(state)
    out=run_research_agent(question=sample['question'],sources=sources,model=model,context=ctx,as_of=CLOCK,
                           clock={'utc_date':'2026-11-22'},checkpoint=checkpoint)
    parent=ResearchVersion(version_id='parent',created_at=CLOCK,content=ResearchContent(),review='draft')
    meta={'turn_id':'exercise','index':1,'question':sample['question'],'question_origin':'user',
          'operation_ids':[],'parent_version_id':'parent','started_at':CLOCK}
    persister=ResearchTurnPersister(topic_id='offline-exercise',parent_version_id='parent',as_of=CLOCK,sources=sources,
                                  version_chain=SimpleNamespace(source_ids=[]),turn_meta=meta)
    content=persister.finalize_turn(operation_id='op-'+case,parent_version=parent,outcome=out,runtime_state=saved,
        context=ctx,finished_at=CLOCK,default_search_status='not_requested',official_fetch_status='not_requested').content
    turn=content.turns[-1]
    receipts=[{k:v for k,v in row.items() if k in ('finish','usage','wire_manifest','cache_telemetry','delivery','transport')}
              for row in ctx.private_diagnostics.values()]
    tools = []
    for exchange in saved.get('history',[]):
        for block in exchange['assistant'].get('content',[]):
            if block.get('type') != 'tool_use':
                continue
            returned = next((r['content'] for r in exchange['results'] if r['tool_call_id']==block['id']), None)
            tools.append({'tool':block['name'],'id':block['id'],'args':block.get('input'), 'receipt':json.loads(returned) if returned else None})
    return {'case':case, 'arm':arm,'model':settings.model,'status':turn.status,'reason':turn.reason,'seconds':round(time.monotonic()-start,2),
        'usage':turn.usage.model_dump(mode='json'),'report':turn.report,'gaps':[g.model_dump() for g in turn.gaps],
        'question_parts':[r.model_dump(mode='json') for r in turn.question_parts], 'facts':[r.model_dump(mode='json') for r in content.facts],
        'calculations':[r.model_dump(mode='json') for r in content.calculations], 'claims':[r.model_dump(mode='json') for r in content.claims],
        'tools':tools,'receipts':receipts}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('case',choices=['J1','J4'])
    parser.add_argument('arm',choices=['A','B'])
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    with args.output.open('x') as output:
        try:
            record=run(args.case,args.arm)
        except Exception as error:
            # Transport or provider exception text may include request details.
            record={'case':args.case,'arm':args.arm,'error_type':type(error).__name__}
        json.dump(record,output,ensure_ascii=False,indent=2)
        output.write('\n')
