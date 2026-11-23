"""Prepare isolated in-memory route fixtures from replay; no provider calls."""
import json, sys, subprocess, os
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from hashlib import sha256
HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[3]
sys.path.insert(0,str(ROOT/'apps/api'))
from fastapi.testclient import TestClient
from app import main
from app.research_topics import ResearchTopic
from app.research_sources import ResearchSource, SourcePermissions, SourceNotFound
from app.research_topics import TopicNotFound
from typing import Any
ResearchTopic.model_rebuild(_types_namespace={"Any": Any})
import inspect
import app.research_topics as rt
from pydantic import BaseModel
for obj in vars(rt).values():
    if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel:
        obj.model_rebuild(_types_namespace={"Any": Any}, raise_errors=False)
NOW=datetime(2026,11,23,tzinfo=timezone.utc)

def build():
    raw_dir=os.environ.get('REPAIR_RAW_RESULTS_DIR')
    results=[] if raw_dir else json.loads(subprocess.check_output([sys.executable,str(HERE/'replay.py')]))
    cases={x['id']:x for x in json.loads((HERE.parent/'cases.json').read_text())['cases']}
    baseline=json.loads((HERE/'frozen-baseline.json').read_text())
    if raw_dir:
        files=sorted(Path(raw_dir).glob('*.json'))
        baseline={'rows':[{'run':p.stem,'path':str(p.resolve())} for p in files]}
        results=[{'parts':json.loads(p.read_text()).get('question_parts') or []} for p in files]
    topics={}; sources={}
    for entry, delivery in zip(baseline['rows'], results):
        run=json.loads(Path(entry['path']).read_text() if raw_dir else (ROOT.parent/entry['path']).read_text()); state=run['final_state_projection']; case=cases[run['case']]
        assert sha256(case['snapshot'].encode()).hexdigest() == run['source']['content_sha256'], f"source hash mismatch: {entry['run']}"
        sid='exhibit'; source=ResearchSource(source_id=sid,owner_id=entry['run'],title='Synthetic exhibit',text=case['snapshot'],kind='user_supplied',obtained_at=NOW,content_sha256=run['source']['content_sha256'],permissions=SourcePermissions(**run['source']['permissions']))
        sources[entry['run']]=source
        turn=dict(turn_id='turn_1',index=1,question=case['question'],question_origin='user',operation_ids=['op_1'],parent_version_id='v1',started_at=NOW,finished_at=NOW,status=run['status'],reason=run['reason'],report=run.get('report') or '',partial_notes=run.get('partial_notes') or None,fact_ids=[f['fact_id'] for f in state.get('facts') or []],calculation_ids=[c['calculation_id'] for c in state.get('calculations') or []],claim_ids=[c['claim_id'] for c in state.get('claims') or []],question_parts=delivery['parts'],usage={'model_calls':run['calls'],'search_calls':run['search_calls'],'total_tokens':run['budget_accounted_tokens'],**run['usage'],'usage_status':'partial_unknown' if run['usage']['unknown_usage_calls'] else 'complete'},search_status='not_requested',official_fetch_status='not_requested',read_ranges=[{'source_id':r['source_id'],'character_start':r['start'],'character_end':r['end']} for r in state.get('read_ranges') or []])
        content=dict(sources=[run['source']],facts=state.get('facts') or [],calculations=state.get('calculations') or [],claims=state.get('claims') or [],turns=[turn])
        topics[entry['run']]=ResearchTopic.model_validate(dict(topic_id=entry['run'],owner_id=entry['run'],request=dict(question=case['question'],source_ids=[sid]),created_at=NOW,updated_at=NOW,versions=[dict(version_id='v2',created_at=NOW,content=content,review='draft',created_by='agent',source_ids=[sid])]))
    return topics,sources

def install(topics,sources):
    main._authenticated_account=lambda authorization: SimpleNamespace(account_id=authorization or '')
    class Topics:
        def list(self,owner):
            return [topics[owner].model_copy(deep=True)] if owner in topics else []
        def get(self,owner,id):
            if owner != id or id not in topics: raise TopicNotFound()
            return topics[id].model_copy(deep=True)
    class Sources:
        def get(self,owner,id):
            if owner not in sources or id != 'exhibit': raise SourceNotFound()
            return sources[owner]
    main.research_topics=Topics();main.research_sources=Sources()

if __name__=='__main__':
    topics,sources=build();install(topics,sources); client=TestClient(main.app); out=[]
    for id in topics:
        headers={'Authorization':id}; a=client.get('/v2/research/topics/'+id,headers=headers); b=client.get('/v2/research/topics/'+id+'/versions/v2/export',headers=headers)
        out.append({'run':id,'topic_status':a.status_code,'export_status':b.status_code,'topic':a.json(),'export':b.json()})
    print(json.dumps(out,ensure_ascii=False,indent=2,default=str))
