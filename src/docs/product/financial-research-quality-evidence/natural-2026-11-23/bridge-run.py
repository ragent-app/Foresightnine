"""Controlled local HTTP research runs with two developer-operated native model peers."""
import json, os, socket, sys, time, uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

root = Path(__file__).resolve().parent
api_root = '/home/ubuntu/Foresightnine/src/apps/api'
sys.path.insert(0, api_root)
os.environ.update(FORESIGHTNINE_LOCAL_AUTH_MODE='mock', FORESIGHTNINE_GDELT_ENABLED='0')
def deny(*_, **__):
    raise RuntimeError('No external network in controlled run')
socket.socket.connect = socket.getaddrinfo = deny
from fastapi.testclient import TestClient
import app.main as api
from app.model_protocol import ModelResponse, ModelToolCall

cases = [
 ('index', '指数在季度审核时把 B 级债券纳入吗？如果成分券在两次审核之间从 BBB 降到 B，什么时候退出？',
  '样例债券指数规则（本实验合成材料）', '本指数在每个季度的最后一个营业日审核。新纳入的债券在审核日必须评级至少为 BBB。已有成分券在两个审核日之间评级降到 BBB 以下，保留至下一次季度审核的收盘时移除。出现发行人违约的，当日收盘移除。'),
 ('household', '十二天后我必须缴纳学费，这是唯一的一笔备用金。甲账户和乙产品都宣传可赎回，这笔钱放哪一个更合适？',
  '样例账户与产品条款（本实验合成材料）', '甲账户的可用余额可于任一自然日按账面本金即时提取，没有提前提取费。乙产品按当日净值赎回，净值可能低于投入本金；赎回须提前三十个自然日申请。')
]
class Bridge:
    name='development-bridge'
    policy=SimpleNamespace(timeout_seconds=240,max_input_tokens=None,max_output_tokens=1800)
    def __init__(self):
        self.counts={'writer':0,'reviewer':0}
    def complete_turn(self, request, **kwargs):
        role='writer' if request.tools else 'reviewer'
        self.counts[role]+=1
        prefix=root/role/f'{self.counts[role]:03d}'
        staging=prefix.with_suffix('.request.tmp')
        staging.write_text(json.dumps(asdict(request),ensure_ascii=False))
        staging.rename(prefix.with_suffix('.request.json'))
        response=prefix.with_suffix('.response.json')
        until=time.monotonic()+235
        while not response.exists() and time.monotonic()<until:
            time.sleep(.15)
        raw=response.read_text()
        if len(raw)>6000:
            raise ValueError('Development response exceeds fixed character limit')
        payload=json.loads(raw)
        if role=='reviewer':
            return ModelResponse({'role':'assistant','content':json.dumps(payload,ensure_ascii=False)},(),None,'stop')
        calls=tuple(ModelToolCall(f'{prefix.stem}-{n}',c['name'],c['arguments'],json.dumps(c['arguments'],ensure_ascii=False)) for n,c in enumerate(payload.get('calls',[])))
        msg={'role':'assistant','content':payload.get('content',''),'tool_calls':[{'id':c.id,'type':'function','function':{'name':c.name,'arguments':c.raw_arguments}} for c in calls]}
        return ModelResponse(msg,calls,None,'tool_calls' if calls else 'stop','tool_calls' if calls else 'end')

client=TestClient(api.app)
a=client.post('/v1/auth/local/register',json={'email':uuid.uuid4().hex+'@example.test','password':'correct horse battery staple!'})
a.raise_for_status()
headers={'Authorization':'Bearer '+a.json()['access_token']}
model=Bridge()
api.challenge_provider=model
try:
    for cid,q,title,doc in cases:
        start_counts=model.counts.copy()
        source=client.post('/v2/research/sources',headers=headers,json={'title':title,'text':doc,'permissions':dict.fromkeys(('store','model_input','display','export'),True)})
        source.raise_for_status()
        s=client.post('/v2/research/topics',headers=headers,json={'question':q,'source_ids':[source.json()['source_id']]})
        s.raise_for_status()
        topic=s.json(); path='/v2/research/topics/'+topic['topic_id']
        t=time.monotonic()
        run=client.post(path+'/runs',headers={**headers,'Idempotency-Key':uuid.uuid4().hex},json={'parent_version_id':topic['versions'][0]['version_id'],'discover_sources':False,'max_model_calls':10})
        run.raise_for_status()
        op=run.json()['operation']['operation_id']
        for _ in range(450):
            operation=client.get(path+'/runs/'+op,headers=headers).json()['operation']
            if operation['status'] in ('completed','failed','cancelled'):break
            time.sleep(2)
        version=client.get(path,headers=headers).json()['versions'][-1]
        export=client.get(path+'/versions/'+version['version_id']+'/export',headers=headers)
        turn=next(iter(version['content']['turns'][-1:]),{})
        result={'case':cid,'question':q,'material':doc,'operation':operation['status'],'turn_status':turn.get('status'),'reason':turn.get('reason'),'report':turn.get('report'),'gaps':turn.get('gaps'),'reviews':turn.get('semantic_reviews'),'requirement_plan':turn.get('requirement_plan'),'parts':turn.get('question_parts'),'claims':version['content']['claims'],'export_http':export.status_code,'counts':{k:model.counts[k]-v for k,v in start_counts.items()},'elapsed_seconds':round(time.monotonic()-t,1)}
        (root/(cid+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2))
finally:
    for role in model.counts: (root/role/'DONE').touch()
